#!/usr/bin/env python3
"""Wake word -> utterance -> transcript -> spoken reply.

The loop, with no agent behind it yet: it says back what it heard, which is
enough to feel the timing and to surface the echo problem early.

Two recognisers, because they are good at different things:

  Vosk, grammar-restricted to the wake phrase, listens continuously. It is
  cheap enough to leave running and its grammar pins the trigger far harder
  than a general model would. It also endpoints your speech and its partial
  results put something on screen immediately.

  Whisper transcribes the captured utterance once you stop. Far more accurate,
  especially with the vocabulary prompt, but it cannot start until you finish,
  so it never runs while you are still talking.

Knobs come from shell.json (written by the bar widget), falling back to
daemon/agentvoice.toml. Both are re-read between utterances.

    python daemon/wake_listen.py
    python daemon/wake_listen.py --simulate bench/corpus/wav/11.wav
    agentvoice toggle          # or: kill -USR1 $(cat $XDG_RUNTIME_DIR/agentvoice/pid)
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from adapters import (  # noqa: E402
    explain as explain_agent, load as load_adapter, omarchy_default, sentences,
)
from adapters.base import speech_safe  # noqa: E402
from speech_text import is_stop_command  # noqa: E402
from runtime import RUNTIME_DIR, Config, Speaker, StateFile  # noqa: E402

from paths import (  # noqa: F401
    ROOT, endpoint_key, project_dir, vocab_file, vosk_model,
)

RATE, CHUNK = 16_000, 3200          # 100ms frames


def _host(url: str) -> str:
    """The host of an endpoint URL, for looking its key up by.

    Port included when there is one, because one machine can run more than
    one endpoint and they do not share a key: Ollama on :11434 needs none,
    while Hermes on :8642 takes a bearer token that its own documentation
    describes as equivalent to a root password. Keying on the hostname alone
    would hand the second one's key to the first.

    A URL with no port keeps the bare hostname, so entries filed before this
    -- api.openai.com and the rest -- still resolve.
    """
    import urllib.parse
    parts = urllib.parse.urlparse(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return f"{host}:{port}" if host and port else host

DIM, RED, GRN, YEL, CYA, BLD, OFF = (
    "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[1m", "\033[0m")


def frame_db(pcm: bytes) -> float:
    """RMS of one frame in dBFS. Cheap: this runs on every 100ms of audio."""
    import numpy as np
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    if not len(x):
        return -99.0
    return float(20.0 * np.log10(np.sqrt((x ** 2).mean()) / 32768.0 + 1e-9))


def vocab_stamp() -> tuple[str, float]:
    """Which vocabulary file, and when it last changed.

    Both halves matter. Editing the list rewrites the file, and creating
    ~/.config/agentvoice/vocab.txt for the first time changes *which* file is
    used -- the shipped default until then. Either way nothing about the
    settings changes, so a daemon that read the list once went on prompting
    Whisper with the old terms until it was restarted.
    """
    path = vocab_file()
    try:
        return str(path), path.stat().st_mtime
    except OSError:
        return str(path), 0.0


def load_vocab() -> str | None:
    path = vocab_file()
    if not path.exists():
        return None
    terms = [l.strip() for l in path.read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    return ", ".join(terms) + "." if terms else None


class OwwWake:
    """openWakeWord detector.

    Unlike the Vosk grammar, this scores acoustically rather than deciding
    between the phrase and everything-else -- which is why it can separate a
    phonetic neighbour at all. Measured here: the real phrase peaks ~0.996, a
    near miss ~0.898, unrelated speech 0.000.

    openWakeWord wants 1280-sample frames (80ms) and the capture loop hands
    out 1600 (100ms), so audio is rebuffered rather than resized upstream --
    the 100ms cadence is what the Whisper path and the level meter expect.
    """

    FRAME = 1280

    def __init__(self, model: str, threshold: float, verifier: str | None = None,
                 use_verifier: bool = True):
        import numpy as np
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from verifier import installed_verifiers, resolve_model

        from openwakeword.model import Model

        path, self.key = resolve_model(model)
        kwargs = {}
        if not use_verifier:
            verifier = ""
        if verifier is None:
            found = installed_verifiers().get(self.key)
            verifier = str(found) if found else None
        if verifier:
            kwargs = {"custom_verifier_models": {self.key: verifier},
                      "custom_verifier_threshold": 0.1}
        self.verifier = verifier
        self.threshold = threshold
        self._np = np
        self._model = Model(wakeword_model_paths=[str(path)], **kwargs)
        self._tail = np.empty(0, dtype=np.int16)
        self.phrase = model.replace("_", " ")
        #: Highest score seen in the most recent feed(), for the monitor.
        self.last_score = 0.0

    def reset(self) -> None:
        self._tail = self._np.empty(0, dtype=self._np.int16)
        if hasattr(self._model, "reset"):
            self._model.reset()

    def feed(self, pcm: bytes) -> bool:
        x = self._np.frombuffer(pcm, dtype=self._np.int16)
        self._tail = self._np.concatenate((self._tail, x))
        fired = False
        self.last_score = 0.0
        while len(self._tail) >= self.FRAME:
            frame, self._tail = self._tail[:self.FRAME], self._tail[self.FRAME:]
            best = max(self._model.predict(frame).values())
            self.last_score = max(self.last_score, best)
            if best >= self.threshold:
                fired = True
        if fired:
            self.reset()
        return fired


class Pipeline:
    def __init__(self, cfg: Config, whisper_override: str | None = None):
        import vosk
        vosk.SetLogLevel(-1)

        self._vosk = vosk
        self._model = None      # loaded on first use; see vosk_model_lazy()
        self._vocab = load_vocab()
        self._vocab_stamp = vocab_stamp()
        self.phrase = ""
        self.engine = "vosk"
        self._oww = None
        self._loud_frames = 0
        self._refractory_until = 0.0
        self._whisper = None
        self._whisper_size = None
        self._whisper_override = whisper_override
        self.apply(cfg)

    def apply(self, cfg: Config) -> None:
        """Pick up the current knobs. Called between utterances only, so a
        turn in progress is never disturbed."""
        engine = cfg.str("engine")
        # The verifier's mtime belongs in the spec: retraining rewrites that
        # file, and without it the daemon keeps the copy it loaded at startup
        # for ever. A freshly trained verifier then appears to do nothing --
        # or worse, a replaced bad one goes on suppressing every wake.
        want = (engine,
                cfg.str("owwModel") if engine == "openwakeword"
                else cfg.str("phrase").lower(),
                self._verifier_stamp(cfg) if engine == "openwakeword" else 0.0,
                # In the spec, not assigned after the fact: openWakeWord takes
                # the verifier as a constructor argument, so turning it off
                # means building the engine without it. The threshold is the
                # opposite case -- a number the running engine can be handed.
                cfg.bool("useVerifier") if engine == "openwakeword" else False)
        if want != getattr(self, "_wake_spec", None):
            self._wake_spec = want
            self._build_wake(cfg, engine)

        self.lead_in_ms = cfg.int("leadInMs")
        self.trailing_ms = cfg.int("trailingSilenceMs")
        self.min_utterance_ms = cfg.int("minUtteranceMs")
        self.max_utterance_ms = cfg.int("maxUtteranceMs")
        self.threshold_db = float(cfg["micThresholdDb"])
        # Assigned here rather than added to the spec above, like every other
        # knob on this list: it needs a different number, not a different model,
        # and a rebuild would drop and reload ~100MB for a slider move.
        #
        # It was the one openWakeWord knob that lived on the engine object and
        # was never updated afterwards. Dragging the slider printed "knobs
        # updated" and changed nothing until the service restarted -- so a
        # detection threshold of 90, which a locally trained model cannot reach,
        # survived being set to 63 and then 51, and the wake word never fired.
        # The accidental cure was switching engines and back, because that does
        # change the spec, which rebuilt the engine at whatever the number had
        # become by then.
        if self._oww is not None:
            self._oww.threshold = cfg.int("owwThresholdPct") / 100.0
        self.refractory_ms = int(cfg["refractoryMs"])
        self.wake_confidence = float(cfg["wakeConfidence"])
        # Changing the transcription model used to be a silent no-op: it was
        # built once in __init__ and never consulted again, so the setting
        # appeared to do nothing until the service restarted.
        wanted = self._whisper_override or cfg.str("model")
        if wanted != self._whisper_size:
            self._load_whisper(wanted)

        self._partials = cfg.str("livePartials")
        self.conversation = cfg.bool("conversationMode")
        self.follow_up_ms = cfg.int("followUpMs")
        # 300ms of continuous speech-level audio before a partial may wake it.
        self.min_loud_frames = 3

    @property
    def detection_threshold(self) -> float | None:
        """openWakeWord's firing threshold, or None on any other engine."""
        return self._oww.threshold if self._oww is not None else None

    @staticmethod
    def _verifier_stamp(cfg: Config) -> float:
        """Modification time of the verifier this engine would load, or 0."""
        try:
            from verifier import installed_verifiers, resolve_model
            _, key = resolve_model(cfg.str("owwModel"))
            found = installed_verifiers().get(key)
            return found.stat().st_mtime if found else 0.0
        except Exception:
            return 0.0

    def _build_wake(self, cfg: Config, engine: str) -> None:
        """Swap the wake engine. Vosk takes any phrase and scores phonetic
        neighbours as high as the real one; openWakeWord has four pretrained
        phrases (plus anything trained locally) and real rejection."""
        self.engine = engine
        if engine == "openwakeword":
            try:
                self._oww = OwwWake(cfg.str("owwModel"),
                                    cfg.int("owwThresholdPct") / 100.0,
                                    use_verifier=cfg.bool("useVerifier"))
                self.phrase = self._oww.phrase
                if self._oww.verifier:
                    verifier = "with your verifier"
                elif not cfg.bool("useVerifier"):
                    # Said plainly, because "no verifier yet" would describe a
                    # trained one that is switched off as though none existed.
                    verifier = "anyone may wake it -- your verifier is off"
                else:
                    verifier = "no verifier yet"
                print(f"  {DIM}wake: openWakeWord {self._oww.key} "
                      f"@ {self._oww.threshold:.2f} ({verifier}){OFF}")
                # Nothing needs Vosk on this engine unless partials are
                # explicitly on. Give the memory back rather than hold it for
                # an engine the user has just left.
                if getattr(self, "_partials", "auto") != "on":
                    self._drop_vosk()
                return
            except Exception as e:
                print(f"  {YEL}openWakeWord unavailable ({type(e).__name__}: {e});"
                      f" falling back to Vosk{OFF}")
                self.engine = "vosk"
        if self._oww is not None:
            self._oww = None
            self._release()
        self.phrase = cfg.str("phrase").lower()
        self._reset_wake()

    @staticmethod
    def _release() -> None:
        """Hand freed pages back to the OS.

        Dropping the last reference to a model is not enough: glibc keeps the
        freed arenas and RSS barely moves. Measured here -- releasing the Vosk
        model returns 67MB on the drop alone and 117MB after malloc_trim;
        openWakeWord 57MB and 71MB. Without the trim an engine swap looks
        exactly like a leak.
        """
        import ctypes
        import gc
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass        # not glibc, or no malloc_trim; the gc still happened

    def _drop_vosk(self) -> None:
        if self._model is None:
            return
        self._wake = None
        self._model = None
        self._release()

    def reconcile_vocab(self) -> None:
        """Re-read the term list if the file behind it has changed.

        Cheap: one stat, and the prompt is passed per transcription rather than
        baked into the model, so nothing is rebuilt. Called on the same poll as
        the voice, not behind the config-changed guard -- editing a file is not
        a setting change.
        """
        stamp = vocab_stamp()
        if stamp == self._vocab_stamp:
            return
        self._vocab_stamp = stamp
        self._vocab = load_vocab()
        n = len(self._vocab.split(",")) if self._vocab else 0
        print(f"  {CYA}vocabulary: {n} terms{OFF}")

    def _load_whisper(self, size: str) -> None:
        """(Re)build the transcription model, releasing the previous one."""
        from faster_whisper import WhisperModel
        if self._whisper is not None:
            self._whisper = None
            self._release()
        print(f"  {DIM}loading whisper {size}...{OFF}", end="", flush=True)
        self._whisper = WhisperModel(size, device="cpu",
                                     compute_type="int8", cpu_threads=4)
        self._whisper_size = size
        n = len(self._vocab.split(",")) if self._vocab else 0
        print(f"\r  {DIM}whisper {size} ready"
              f"{f', {n} vocab terms' if n else ''}{OFF}{' ' * 24}")

    def vosk_model_lazy(self):
        """The Vosk acoustic model, loaded the first time something needs it.

        It costs 156MB resident -- a fifth of the daemon -- and on the
        openWakeWord engine it buys only the live partial text shown while you
        speak. Endpointing is energy-based and does not need it. So it is no
        longer loaded just in case.
        """
        if self._model is None:
            found = vosk_model()
            if found is None:
                raise FileNotFoundError(
                    "no Vosk model installed — run install.sh, or use the "
                    "openWakeWord engine, which needs no separate download")
            self._model = self._vosk.Model(str(found))
        return self._model

    def _reset_wake(self) -> None:
        self._wake = self._vosk.KaldiRecognizer(
            self.vosk_model_lazy(), RATE, json.dumps([self.phrase, "[unk]"]))
        # Word-level confidence is the second line of defence. A grammar has
        # to choose between the phrase and [unk], so anything phonetically
        # near the trigger scores as a match -- which is how a podcast on a
        # phone across the room wakes it.
        self._wake.SetWords(True)

    def feed_wake(self, pcm: bytes, level_db: float) -> bool:
        """True when the trigger phrase lands loudly and confidently enough.

        Three gates, because any one of them alone is too easy to trip:
        the frame has to be louder than the room, the phrase has to survive a
        sustained run of such frames rather than one blip, and the recogniser
        has to be reasonably sure of the words it matched.
        """
        # A detector's internal buffers still hold the audio that just fired,
        # so the next few frames can fire again on the same utterance.
        # reset() does not fully clear them -- measured: feeding an unrelated
        # clip straight after a positive one still fired. A refractory window
        # is the reliable guard.
        if time.time() < self._refractory_until:
            return False

        # Measured cost of doing this, on the 2014 dev machine: openWakeWord
        # inference is 2.97ms of CPU per 80ms frame -- 3.7% of one core -- and
        # the whole daemon idles at 5.3%. Cheap enough that gating it to save
        # CPU was never worth the detection it cost.
        #
        # openWakeWord's own vad_threshold does NOT help here. It runs Silero
        # VAD *after* the predictions are computed and zeroes them when the VAD
        # disagrees, so it adds about 22% to the inference cost rather than
        # saving any. It is a false-accept tool, and not even the right one for
        # the case we care about: a podcast saying the wake phrase is speech,
        # so a VAD passes it happily.
        #
        # openWakeWord is fed EVERY frame, gate or no gate. It scores
        # acoustically and rejects unrelated speech on its own -- measured at
        # 0.000 -- so it needs no energy gate, and applying one actively
        # breaks it: the model builds embeddings over a sliding window of
        # contiguous audio, and a gate at -36 dBFS discarded 70% of the frames
        # in a recorded "hey jarvis", fragmenting the phrase. Measured on the
        # same twelve clips: 9/12 detected through the gate, 12/12 without it,
        # and every peak rose to 1.00.
        #
        # The gate below still guards the Vosk grammar, which does need it --
        # a grammar will happily match the phrase against room noise.
        if self._oww is not None:
            if self._oww.feed(pcm):
                self._refractory_until = time.time() + self.refractory_ms / 1000.0
                return True
            return False

        if level_db < self.threshold_db:
            self._loud_frames = 0
            return False
        self._loud_frames += 1

        final = self._wake.AcceptWaveform(pcm)
        if final:
            payload = json.loads(self._wake.Result())
            text = payload.get("text", "")
            words = [w for w in payload.get("result", [])
                     if w.get("word") in self.phrase.split()]
            conf = (sum(w.get("conf", 0.0) for w in words) / len(words)) if words else 0.0
        else:
            text = json.loads(self._wake.PartialResult()).get("partial", "")
            conf = None            # partials carry no confidence

        if self.phrase not in text:
            return False

        # A partial can only wake it after enough consecutive loud frames to
        # be actual speech; a final also has to clear the confidence bar.
        if conf is None:
            if self._loud_frames < self.min_loud_frames:
                return False
        elif conf < self.wake_confidence:
            print(f"  {DIM}(rejected \"{text}\" — confidence {conf:.2f}"
                  f" < {self.wake_confidence:.2f}){OFF}")
            self._reset_wake()
            self._loud_frames = 0
            return False

        self._reset_wake()
        self._loud_frames = 0
        self._refractory_until = time.time() + self.refractory_ms / 1000.0
        return True

    def new_capture(self):
        """A recogniser for the live partial text, or None when it is not worth
        the memory. The caller must cope with None: the turn still works, it
        just shows nothing until Whisper returns."""
        if not self.want_partials:
            return None
        return self._vosk.KaldiRecognizer(self.vosk_model_lazy(), RATE)

    @property
    def want_partials(self) -> bool:
        # "auto" means free-only: show partials when the Vosk model is already
        # resident for the wake word, and do not load it purely for them.
        if self._partials == "off":
            return False
        if self._partials == "on":
            return True
        return self.engine == "vosk"

    def transcribe(self, pcm: bytes) -> tuple[str, float]:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        segments, _ = self._whisper.transcribe(
            audio, language="en", beam_size=1, initial_prompt=self._vocab)
        text = " ".join(s.text for s in segments).strip()
        return text, (time.perf_counter() - t0) * 1000


def _empty_turn() -> dict:
    """Nothing heard yet, nothing answered.

    Published the instant the wake word fires. The capture state used to carry
    whatever the *previous* turn had left in it, so saying the wake word showed
    the last question and the last answer while it listened for the next one.
    A readout of a finished exchange is worse than no readout: it is the right
    shape in the wrong tense.

    All four keys are always present, because publish() merges what it is given
    and the widget only updates a field it is sent -- an absent key leaves the
    old value on screen rather than clearing it.
    """
    return {"transcript": "", "ms": 0, "audio_s": 0.0, "reply": ""}


class Daemon:
    def __init__(self, cfg: Config, pipe: Pipeline, state: StateFile,
                 speaker: Speaker | None, agent=None):
        self.cfg, self.pipe, self.state, self.speaker = cfg, pipe, state, speaker
        self.agent = agent
        # Carried between turns so the agent remembers the conversation.
        self.session_id: str | None = None
        self.enabled = True
        # Half duplex. One microphone six inches from the speakers has no
        # chance against its own output, so the microphone is simply deaf
        # while the machine talks -- and for a moment afterwards, because a
        # room has reverb and the audio device has a buffer.
        self._frames = None
        self._deaf_until = 0.0
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / "pid").write_text(str(os.getpid()))
        # Barge-in is not possible acoustically on this class of hardware:
        # at a normal listening volume our own playback reaches the mic at
        # the same level as the user's voice (SNR +1.2 dB), and detection
        # falls to 5/20. PipeWire's echo canceller fixes that but needs ~30s
        # of continuous output to converge, which is longer than a reply. So
        # an interrupt is an explicit act -- a keybind, or the Stop button.
        self._interrupt = threading.Event()
        self._barge = None
        signal.signal(signal.SIGUSR1, self._on_toggle)
        signal.signal(signal.SIGUSR2, self._on_interrupt)

    def _on_toggle(self, *_):
        self.enabled = not self.enabled
        if not self.enabled:
            if self.speaker:
                self.speaker.cancel()
            if self.agent:
                self.agent.cancel()
        print(f"\n  {CYA}mic {'engaged' if self.enabled else 'released'}{OFF}")
        self._publish()

    def reconcile_agent(self) -> None:
        """Rebuild the agent when the project or permission level changes.

        The adapter is constructed once at startup, so without this a new
        directory would be set in the UI and simply not happen -- the same
        read-once bug that made two earlier settings look broken.

        The session is dropped on the way: `--resume` is scoped to a project
        in Claude Code, so carrying an id across a directory change would
        resume the wrong conversation, or none.
        """
        endpoint = self.endpoint_spec()
        want_name = "endpoint" if endpoint else omarchy_default()
        want_cwd = str(project_dir(self.cfg.str("projectDir")))
        want_level = self.cfg.level_for(want_name)
        want_ask = want_level != "trusted"
        # The endpoint's identity is its URL and model, not just its name:
        # switching which machine answers must rebuild, and they all report
        # the same name.
        same_endpoint = (endpoint is None
                         or (getattr(self.agent, "base_url", None)
                             == endpoint["url"].rstrip("/")
                             and getattr(self.agent, "model", None)
                             == endpoint["model"]
                             and getattr(self.agent, "is_agent", False)
                             == endpoint["is_agent"]))
        if (self.agent is not None
                and getattr(self.agent, "name", None) == want_name
                and same_endpoint
                and (endpoint is not None
                     or getattr(self.agent, "cwd", None) == want_cwd)
                and (endpoint is not None
                     or getattr(self.agent, "ask_permission", None) == want_ask)):
            self.publish_agent()
            return
        if self.agent is not None:
            self.agent.cancel()
        if endpoint:
            endpoint = {**endpoint, "key": endpoint_key(_host(endpoint["url"]))}
        self.agent = load_adapter(ask_permission=want_ask, cwd=want_cwd,
                                  level=want_level, endpoint=endpoint)
        self.session_id = None
        self.publish_agent()
        where = f" · {want_cwd}" if getattr(self.agent, "has_tools", True) else ""
        print(f"  {CYA}agent: {want_name or 'nobody'}{where}{OFF}"
              f"  {DIM}({self.posture()}){OFF}")
        chosen = omarchy_default()
        if want_name == "endpoint" and chosen:
            print(f"  {YEL}the endpoint setting is answering instead of "
                  f"{chosen} — clear it to use the desktop's agent{OFF}")
        # Said here as well as in the banner, because the banner only reprints
        # when our own config changes and switching agents writes Omarchy's
        # file -- so on the one occasion this matters most, it would not run.
        if (self.agent is not None and self.pipe is not None
                and getattr(self.pipe, "conversation", False)
                and not getattr(self.agent, "remembers", False)):
            print(f"  {YEL}conversation is on, but {self.agent.name} starts "
                  f"fresh every turn — it saves you the wake word and "
                  f"nothing more{OFF}")

    def endpoint_spec(self) -> dict | None:
        """The configured OpenAI-compatible endpoint, or None.

        The key never travels with the rest of the settings: it is read from
        its own file at the moment it is needed, so it cannot end up in
        shell.json, in the state file, or in anything published to the panel.
        """
        url = self.cfg.str("endpointUrl").strip()
        model = self.cfg.str("endpointModel").strip()
        if not url or not model:
            return None
        # No key here: this runs on the idle poll, every couple of seconds,
        # and reading the keyring means spawning secret-tool. The key is
        # fetched once, where the adapter is actually built.
        return {"url": url, "model": model,
                "is_agent": self.cfg.bool("endpointIsAgent")}

    def level(self) -> str:
        return self.cfg.level_for(getattr(self.agent, "name", None))

    def posture(self) -> str:
        """What the level means for the agent actually running."""
        if self.agent is None:
            return "no agent"
        return self.agent.posture(self.level())

    def publish_agent(self) -> None:
        """Tell the panel which agent is up and what it can honour.

        The panel could not work this out for itself without keeping its own
        copy of every adapter's capabilities, which would drift the first time
        one changed. It is published instead, so the screen and the daemon
        cannot disagree about a permission guarantee.
        """
        if self.agent is None:
            # Echoing instead of answering looks like a broken assistant. Say
            # which agent is missing and why, where it can actually be read.
            name = omarchy_default() or ""
            self.state.describe(agent=name, level="ask", levels=["ask"],
                                posture=explain_agent(name) or "no agent")
            return
        # An endpoint takes precedence over the desktop's agent, which is a
        # reasonable rule and an unreasonable surprise: choosing Claude in
        # Omarchy's settings and having nothing change is indistinguishable
        # from the setting being broken. Name what is being overridden.
        chosen = omarchy_default() or ""
        endpoint_wins = getattr(self.agent, "name", "") == "endpoint" and chosen
        self.state.describe(
            agent=getattr(self.agent, "name", "") or "",
            level=self.level(),
            levels=list(getattr(self.agent, "levels", ("ask", "trusted"))),
            posture=self.posture(),
            remembers=bool(getattr(self.agent, "remembers", False)),
            overriding=chosen if endpoint_wins else "",
            # Whether the project directory and permission level reach the
            # running backend at all. They govern a CLI started here, in a
            # directory we choose, under flags we pass. They govern nothing
            # about a machine down the hall.
            governed=bool(getattr(self.agent, "cwd", None)),
            # The daemon has always known this and only ever said it in a log
            # line nobody reads. A verifier is the one layer that knows who is
            # talking, so whether it is in force belongs on screen.
            verifier=bool(getattr(getattr(self.pipe, "_oww", None),
                                  "verifier", None)),
        )

    def _on_interrupt(self, *_):
        """Cut the current reply short and go back to listening.

        Only meaningful mid-turn; when the daemon is already idle this is
        deliberately a no-op, so a stray keypress cannot leave it in a state
        the user never asked for.
        """
        if self.state.current not in ("transcribing", "thinking", "speaking"):
            return
        self._interrupt.set()
        if self.speaker:
            self.speaker.cancel()
        if self.agent:
            self.agent.cancel()
        print(f"\n  {YEL}interrupted{OFF}")

    def take_command(self) -> str | None:
        """One-shot commands from the CLI, for what a signal cannot carry.

        Push-to-talk needs press and release as two distinct events, which is
        more signals than they are worth. A file is atomic enough when the
        writer renames it into place and the reader unlinks it. Interrupts
        stay on a signal, because the run loop is blocked inside playback
        exactly when an interrupt matters and would never read this.
        """
        path = RUNTIME_DIR / "command"
        if not path.exists():
            return None
        try:
            cmd = path.read_text().strip()
            path.unlink()
            return cmd
        except OSError:
            return None

    def _publish(self, **extra):
        self.state.publish("listening" if self.enabled else "off", **extra)

    def banner(self):
        p = self.pipe
        print(f"\n  {BLD}listening{OFF} for {CYA}\"{p.phrase}\"{OFF}"
              f"   {DIM}lead-in {p.lead_in_ms}ms · trailing {p.trailing_ms}ms{OFF}")
        # The detection threshold is openWakeWord's and the grammar confidence
        # is Vosk's; printing the second one on the first engine showed a number
        # that did nothing while the number that mattered was invisible after
        # startup -- which is how a stale 0.90 went unnoticed through two
        # attempts to lower it.
        second = (f"detection {p.detection_threshold:.2f}"
                  if p.detection_threshold is not None
                  else f"confidence {p.wake_confidence:.2f}")
        print(f"  {DIM}gate {p.threshold_db:.0f} dBFS · {second} "
              f"· {self.cfg.source}{OFF}")
        forgets = self.agent is not None and not getattr(self.agent, "remembers", False)
        if p.conversation and forgets:
            print(f"  {YEL}conversation: on, {p.follow_up_ms / 1000:.0f}s window — but "
                  f"{self.agent.name} starts fresh every turn, so it saves you the "
                  f"wake word and nothing else{OFF}")
        else:
            print(f"  {DIM}conversation: "
                  f"{f'on, {p.follow_up_ms / 1000:.0f}s follow-up window' if p.conversation else 'off'}"
                  f"{OFF}")
        who = f"{self.agent.name}" if self.agent else "nobody (echo mode)"
        # A setting called "ask before it changes anything" must not quietly
        # mean nothing. Only Claude Code can raise a prompt from here; the
        # others are held in whatever read-only or ask-first mode their CLI
        # has, which is weaker and worth saying out loud.
        if self.agent and not getattr(self.agent, "has_tools", True):
            print(f"  {DIM}permission: agentvoice offers {self.agent.name} no "
                  f"tools, so there is nothing here to permit{OFF}")
        elif self.agent and not getattr(self.agent, "can_be_gated", True):
            print(f"  {YEL}permission: agentvoice cannot restrain this endpoint. "
                  f"It is given no flags, no hook and no sandbox — whatever it "
                  f"is allowed to do on its own host, it will do{OFF}")
        elif self.agent and self.level() != "trusted":
            if getattr(self.agent, "guards_permissions", False):
                print(f"  {DIM}permission: prompts on screen{OFF}")
            else:
                print(f"  {YEL}permission: {self.agent.name} cannot prompt from here."
                      f" Running in its safest mode instead — it may refuse work"
                      f" rather than ask.{OFF}")
        elif self.agent:
            print(f"  {YEL}permission: not asking. {self.agent.name} can change"
                  f" files and run commands unchallenged.{OFF}")
        if self.agent and getattr(self.agent, "cwd", None):
            print(f"  {DIM}project: {self.agent.cwd} · {self.posture()}{OFF}")
        elif self.agent:
            # No cwd to speak of: it never touches the filesystem.
            print(f"  {DIM}{self.posture()}{OFF}")
        print(f"  {DIM}agent: {who} · voice: "
              f"{self.speaker.name if self.speaker else 'off'}"
              f"   (ctrl-c to stop){OFF}\n")

    def refresh(self):
        """Between turns, re-read config and re-announce if it moved."""
        # Outside the reload guard: `omarchy default agent` writes its own
        # file, which cfg.reload() does not watch, so a switch at the desktop
        # would otherwise not reach the daemon until it restarted.
        self.reconcile_agent()
        # Outside the guard for the same reason: a voice arrives on disk when
        # the installer fetches it, and a file appearing changes no setting.
        # Behind the guard, a substitution outlived its own download until
        # something unrelated was touched -- the same shape of stall as the
        # detection threshold that never reached the running engine.
        self.reconcile_voice()
        self.pipe.reconcile_vocab()
        if not self.cfg.reload():
            return
        self.pipe.apply(self.cfg)
        self.reconcile_voice()
        print(f"  {CYA}knobs updated{OFF}")

    def reconcile_voice(self) -> None:
        """Load the voice the settings ask for, if that is not what is loaded.

        Called between turns rather than mid-sentence, because a voice swap
        means loading a different model. Cheap when nothing has changed: every
        branch below is a comparison.
        """
        if not self.cfg.bool("speakReplies"):
            self.speaker = None
            return
        wanted = self.cfg.str("voice")
        if (self.speaker is not None
                and self.speaker.requested == wanted
                and not self.speaker.superseded()):
            return
        try:
            self.speaker = Speaker(wanted)
            if self.speaker.substituted_for:
                print(f"  {YEL}the voice {self.speaker.substituted_for} is not "
                      f"downloaded; speaking as {self.speaker.name}{OFF}")
            else:
                print(f"  {CYA}voice: {self.speaker.name}{OFF}")
        except Exception as e:
            print(f"  {YEL}voice {wanted} unavailable: {e}{OFF}")
        self.banner()

    def deafen(self, tail_ms: int) -> None:
        """Drop everything the microphone captured while we were speaking.

        The run loop is synchronous, so frames pile up in the queue during
        playback. Processing them afterwards would feed our own voice to the
        wake word -- which is exactly how a speaking assistant wakes itself.
        """
        self._deaf_until = time.time() + tail_ms / 1000.0
        if self._frames is None:
            return
        dropped = 0
        while True:
            try:
                self._frames.get_nowait()
                dropped += 1
            except Exception:
                break
        if dropped:
            print(f"  {DIM}(dropped {dropped * 100}ms of echo){OFF}")

    def speak(self, text: str, tail_ms: int) -> None:
        """Say something, then make sure we did not hear ourselves say it."""
        if not self.speaker or self._interrupt.is_set():
            return
        watch = self._barge_watch()
        try:
            self.speaker.say(speech_safe(text), watch=watch)
        finally:
            self.deafen(tail_ms)

    def _barge_watch(self):
        """A callable Speaker checks between chunks, or None when off.

        Off by default and for a measured reason: where the microphone sits
        beside the speaker, every gate that hears the user also fires on our
        own output, because the two arrive at the same level. `agentvoice
        calibrate` says which kind of room this is.
        """
        if not self.cfg.bool("bargeIn") or self._frames is None:
            return None
        if self._barge is None:
            try:
                from barge import BargeIn
                self._barge = BargeIn(factor=self.cfg.int("bargeFactor") / 100.0)
            except Exception as e:
                print(f"  {YEL}barge-in unavailable: {e}{OFF}")
                return None
        self._barge.factor = self.cfg.int("bargeFactor") / 100.0
        self._barge.reset()

        def watch() -> bool:
            # Drain whatever the microphone captured while that chunk played.
            while True:
                try:
                    pcm = self._frames.get_nowait()
                except Exception:
                    return False
                if self._barge.feed(pcm):
                    print(f"  {YEL}heard you over us — stopping{OFF}")
                    self._interrupt.set()
                    if self.agent:
                        self.agent.cancel()
                    return True

        return watch

    def answer(self, text: str, last: dict) -> None:
        """Hand the transcript to the agent and speak the reply as it lands.

        Speech starts on the first complete sentence rather than the whole
        reply, because the agent's own generation is now the slowest thing in
        the loop by an order of magnitude -- local transcription is ~300ms and
        time-to-first-token is measured in seconds.
        """
        tail_ms = self.cfg.int("echoTailMs")
        self._interrupt.clear()
        if not self.agent:
            if self.speaker:
                self.state.publish("speaking", **last)
                self.speak(text, tail_ms)       # echo mode
            return

        self.state.publish("thinking", **last)
        t0 = time.time()
        reply_parts: list[str] = []
        ttft = None
        error = None

        def tap(stream):
            nonlocal ttft, error
            for chunk in stream:
                if chunk.session_id:
                    self.session_id = chunk.session_id
                if chunk.ttft_ms is not None and ttft is None:
                    ttft = chunk.ttft_ms
                if chunk.tool:
                    print(f"  {DIM}[{chunk.tool}]{OFF}", flush=True)
                if chunk.error:
                    error = chunk.error
                yield chunk

        try:
            for sentence in sentences(tap(self.agent.send(text, self.session_id))):
                # Checked before the sentence is recorded, so the transcript
                # in the panel is what was actually said out loud rather than
                # what the agent had written by the time it was stopped.
                if self._interrupt.is_set():
                    break
                print(f"  {CYA}{sentence}{OFF}", flush=True)
                reply_parts.append(sentence)
                # Published per sentence rather than once at the end. The answer
                # used to reach the state file only after the last word was
                # spoken, which is the moment the readout starts counting down
                # to close -- so it was on screen for the linger and nothing
                # more. Publishing here puts the text up as that sentence
                # begins, because speak() below blocks until it has been said.
                last["reply"] = " ".join(reply_parts)[:400]
                if self.speaker and self.enabled:
                    self.state.publish("speaking", **last)
                    self.speak(sentence, tail_ms)
                else:
                    # No voice: the text is the entire answer, so it matters
                    # more here, not less. Keep whatever state we are in.
                    self.state.publish(self.state.current, **last)
        except Exception as e:                  # a broken agent must not end the loop
            error = f"{type(e).__name__}: {e}"

        if self._interrupt.is_set():
            # Killing the agent mid-stream usually surfaces as an error. It is
            # not one, and announcing it would talk over the user who just
            # asked for silence.
            self.deafen(tail_ms)
        elif error:
            print(f"  {YEL}agent error: {error}{OFF}")
            self.speak("Sorry, the agent failed.", tail_ms)

        reply = " ".join(reply_parts)
        last["reply"] = reply[:400]
        print(f"  {DIM}agent {time.time() - t0:.1f}s"
              f"{f', first token {ttft:.0f}ms' if ttft else ''}{OFF}")

    def run(self, device: int | None) -> int:
        import sounddevice as sd

        frames: queue.Queue[bytes] = queue.Queue()

        def cb(indata, _f, _t, status):
            if status:
                print(f"\n  {YEL}[audio: {status}]{OFF}")
            frames.put(bytes(indata))

        self._frames = frames
        self.banner()
        self._publish()
        last = _empty_turn()

        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                              blocksize=CHUNK, device=device, callback=cb):
            phase = "wake"
            buf = b""
            cap = None
            woke_at = last_voice = 0.0
            heard = False
            # A follow-up gets its own, shorter window: after a reply the
            # machine keeps listening for a moment so the next thing you say
            # does not need the wake word again.
            active_lead_in = self.pipe.lead_in_ms

            level_shown = 0.0
            last_poll = time.time()
            # Push-to-talk: the key decides both ends of the turn, so the
            # wake word is bypassed and silence no longer endpoints.
            ptt = ptt_done = False
            while True:
                pcm = frames.get()

                cmd = self.take_command()
                if cmd == "talk" and self.enabled:
                    phase, buf, cap = "capture", b"", self.pipe.new_capture()
                    woke_at = last_voice = time.time()
                    heard = False
                    ptt, ptt_done = True, False
                    active_lead_in = self.pipe.lead_in_ms
                    self._deaf_until = 0.0
                    print(f"  {GRN}● talk{OFF}  {DIM}(listening while held){OFF}",
                          flush=True)
                    last = _empty_turn()
                    self.state.publish("capture", **last)
                elif cmd == "talk-end" and ptt:
                    ptt_done = True

                # Deaf window after our own speech: discard without even
                # measuring, so a tail of reverb cannot register as input.
                if time.time() < self._deaf_until:
                    continue

                level = frame_db(pcm)

                if not self.enabled:
                    continue

                # The meter in the panel is how the threshold becomes settable
                # by eye instead of by guesswork, so the level is published even
                # while nothing is happening -- throttled, since this is every
                # 100ms and the file is watched.
                if phase == "wake" and abs(level - level_shown) > 1.5:
                    level_shown = level
                    self._publish(level_db=round(level, 1), **last)

                # Settings used to land only after the next turn, because the
                # only refresh() was at a turn boundary -- so changing the
                # engine or the model while idle appeared to do nothing until
                # you spoke. Poll while waiting instead; rebuilding a model
                # blocks for a second or two, which is harmless here and
                # unacceptable mid-utterance.
                if phase == "wake" and time.time() - last_poll > 2.0:
                    last_poll = time.time()
                    self.refresh()

                if phase == "wake":
                    if self.pipe.feed_wake(pcm, level):
                        print(f"  {GRN}● wake{OFF}  {DIM}go ahead"
                              f" ({self.pipe.lead_in_ms / 1000:.1f}s to start){OFF}",
                              flush=True)
                        phase, buf, cap = "capture", b"", self.pipe.new_capture()
                        woke_at = last_voice = time.time()
                        heard = False
                        active_lead_in = self.pipe.lead_in_ms
                        last = _empty_turn()
                        self.state.publish("capture", **last)
                    continue

                buf += pcm
                # cap is None when live partials are off -- the turn runs
                # exactly the same, it just shows nothing until Whisper
                # returns. Endpointing below is energy-based either way.
                partial = ""
                if cap is not None and not cap.AcceptWaveform(pcm):
                    partial = json.loads(cap.PartialResult()).get("partial", "")
                # Energy decides when you stopped talking; Vosk's partials only
                # say what it thinks you said. A pause between words produces no
                # new partial but is not the end of a turn.
                if level >= self.pipe.threshold_db:
                    heard = True
                    last_voice = time.time()
                if partial:
                    print(f"\r  {DIM}{partial[:100]}{OFF}{' ' * 20}",
                          end="", flush=True)

                now = time.time()
                elapsed_ms = (now - woke_at) * 1000
                quiet_ms = (now - last_voice) * 1000

                # Waiting for you to begin. Deliberately generous: being cut
                # off before you have started is the most irritating failure
                # this thing can have.
                if not heard:
                    # Holding the key says "I am about to speak", so the
                    # lead-in timer does not run until it comes back up.
                    if ptt_done if ptt else elapsed_ms > active_lead_in:
                        print(f"\r  {DIM}(nothing heard){OFF}{' ' * 40}")
                        phase = "wake"
                        ptt = ptt_done = False
                        self._publish(**last)
                        self.refresh()
                    continue

                if ptt and not ptt_done:
                    # A key that never comes back up must not buffer forever.
                    if elapsed_ms < self.pipe.max_utterance_ms:
                        continue
                elif not ptt and quiet_ms <= self.pipe.trailing_ms \
                        and elapsed_ms < self.pipe.max_utterance_ms:
                    continue
                if elapsed_ms >= self.pipe.max_utterance_ms:
                    print(f"\r  {YEL}(hit max_utterance_ms){OFF}{' ' * 30}")
                ptt = ptt_done = False

                audio_ms = len(buf) / 32.0
                follow_up = False
                if audio_ms < self.pipe.min_utterance_ms:
                    print(f"\r  {DIM}(too short, discarded){OFF}{' ' * 40}")
                else:
                    # Its own state. This phase and the agent's turn both
                    # published "thinking", and the panel labelled that
                    # "TRANSCRIBING" -- so the label was right here and wrong
                    # for the whole of the agent's turn, which is the part
                    # people actually wait through.
                    self.state.publish("transcribing", **last)
                    text, ms = self.pipe.transcribe(buf)
                    print(f"\r{' ' * 120}\r  {BLD}{text or '(nothing)'}{OFF}")
                    print(f"  {DIM}{audio_ms / 1000:.1f}s audio, "
                          f"whisper {ms:.0f}ms{OFF}")
                    last = {**_empty_turn(), "transcript": text,
                            "ms": round(ms),
                            "audio_s": round(audio_ms / 1000, 2)}

                    # "stop", "cancel that", "never mind" and friends discard
                    # the turn instead of sending it. This is not an interrupt
                    # and cannot be one -- by the time anything is being said
                    # aloud the microphone is already deaf. Its real job is
                    # cancelling a wake that fired by mistake. Matched against
                    # the whole transcript, so an ordinary sentence that merely
                    # contains "stop" cannot trigger it.
                    if self._interrupt.is_set():
                        # Stop pressed while Whisper was running. answer()
                        # clears this flag on entry, so the interrupt was
                        # accepted here and then thrown away: the reply arrived
                        # regardless. Discard the turn instead.
                        self._interrupt.clear()
                        print(f"  {DIM}(interrupted before sending){OFF}")
                        follow_up = False
                    elif text and is_stop_command(text):
                        print(f"  {DIM}(stopping){OFF}")
                        follow_up = False
                    elif text:
                        self.answer(text, last)
                        follow_up = self.pipe.conversation
                    else:
                        follow_up = False
                    print()

                if follow_up and self.enabled:
                    phase, buf, cap = "capture", b"", self.pipe.new_capture()
                    woke_at = last_voice = time.time()
                    heard = False
                    active_lead_in = self.pipe.follow_up_ms
                    print(f"  {CYA}● still listening{OFF}  {DIM}"
                          f"({active_lead_in / 1000:.0f}s — or say the wake word again)"
                          f"{OFF}", flush=True)
                    self.state.publish("followup", **last)
                    self.refresh()
                    continue

                phase = "wake"
                self._publish(**last)
                self.refresh()
        return 0


def run_simulated(pipe: Pipeline, speaker: Speaker | None, wav: Path,
                  daemon: "Daemon | None" = None) -> int:
    with wave.open(str(wav)) as w:
        pcm = w.readframes(w.getnframes())
    print(f"\n  {DIM}simulating: {wav.name} ({len(pcm) / 32000:.1f}s)"
          f" — no wake phrase, capturing the whole clip{OFF}\n")
    cap = pipe.new_capture()
    for off in range(0, len(pcm), CHUNK):
        if cap is not None and not cap.AcceptWaveform(pcm[off:off + CHUNK]):
            p = json.loads(cap.PartialResult()).get("partial", "")
            if p:
                print(f"\r  {DIM}{p[:100]}{OFF}", end="", flush=True)
    text, ms = pipe.transcribe(pcm)
    print(f"\r{' ' * 120}\r  {BLD}{text}{OFF}")
    print(f"  {DIM}whisper {ms:.0f}ms{OFF}")
    if text and daemon is not None:
        daemon.answer(text, {"transcript": text, "ms": round(ms), "audio_s": 0.0})
    elif text and speaker:
        print(f"  {DIM}spoke {speaker.say(text):.1f}s{OFF}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper", default=None, help="override the configured model")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--no-speak", action="store_true")
    ap.add_argument("--no-agent", action="store_true",
                    help="echo the transcript instead of asking an agent")
    ap.add_argument("--simulate", type=Path)
    args = ap.parse_args()

    cfg = Config()
    print(f"  {DIM}knobs: {cfg.source}{OFF}")
    pipe = Pipeline(cfg, args.whisper)

    speaker = None
    if cfg.bool("speakReplies") and not args.no_speak:
        try:
            speaker = Speaker(cfg.str("voice"))
            if speaker.substituted_for:
                print(f"  {YEL}the voice {speaker.substituted_for} is not "
                      f"downloaded; speaking as {speaker.name}{OFF}")
        except Exception as e:
            print(f"  {YEL}tts unavailable: {e}{OFF}")

    agent = None
    if not args.no_agent:
        level = cfg.level_for(omarchy_default())
        url = cfg.str("endpointUrl").strip()
        model = cfg.str("endpointModel").strip()
        spec = ({"url": url, "model": model, "key": endpoint_key(_host(url)),
                 "is_agent": cfg.bool("endpointIsAgent")}
                if url and model else None)
        agent = load_adapter(ask_permission=level != "trusted", level=level,
                             cwd=str(project_dir(cfg.str("projectDir"))),
                             endpoint=spec)
        if agent is None:
            which = omarchy_default()
            print(f"  {YEL}no agent: {explain_agent(which)}"
                  f"; echoing instead{OFF}")

    state = StateFile()
    try:
        daemon = Daemon(cfg, pipe, state, speaker, agent)
        if args.simulate:
            return run_simulated(pipe, speaker, args.simulate, daemon)
        return daemon.run(args.device)
    except KeyboardInterrupt:
        print(f"\n  {DIM}stopped{OFF}")
        return 0
    finally:
        state.clear()


if __name__ == "__main__":
    raise SystemExit(main())
