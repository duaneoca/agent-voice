"""Config, state publishing, and speech for the agentvoice prototype.

Three small concerns that the wake loop should not have to know about:

  Config      resolves a knob from shell.json (where the bar widget writes it),
              falling back to the local TOML, then to a built-in default.
  StateFile   publishes what the daemon is doing, for the bar to read.
  Speaker     turns text into audio with Piper.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import tomllib
from pathlib import Path

from paths import (  # noqa: F401
    RUNTIME_DIR, ROOT, SHELL_JSON, config_toml, piper_voices,
)

PLUGIN_ID = "duaneoca.agentvoice"

# Mirrors manifest.json's barWidget.defaults. Duplicated deliberately: the
# daemon has to run with no plugin installed and no config file present.
DEFAULTS = {
    "engine": "vosk",
    "phrase": "hey computer",
    "owwModel": "hey_jarvis",
    "owwThresholdPct": 90,
    "leadInMs": 5000,
    "trailingSilenceMs": 1200,
    "minUtteranceMs": 400,
    "maxUtteranceMs": 30000,
    "micThresholdDb": -38,
    "echoTailMs": 350,
    "refractoryMs": 2000,
    # The float is what the code reads; the percent is what the bar schema
    # exposes, because that schema has no float type. __getitem__ converts.
    "wakeConfidence": 0.70,
    "wakeConfidencePct": 70,
    "model": "tiny.en",
    "speakReplies": True,
    "livePartials": "auto",
    "bargeIn": False,
    "bargeFactor": 150,
    "permissions": {},
    "projectDir": "",
    "endpointUrl": "",
    "endpointModel": "",
    "endpointIsAgent": False,
    "conversationMode": True,
    "followUpMs": 7000,
    "voice": "lessac-medium",
}

# The TOML predates the plugin and uses snake_case under sections. Kept working
# so the loop still runs on a box without Omarchy's shell.
TOML_ALIASES = {
    "phrase": ("wake", "phrase"),
    "engine": ("wake", "engine"),
    "owwModel": ("wake", "oww_model"),
    "owwThresholdPct": ("wake", "oww_threshold_pct"),
    "leadInMs": ("timing", "lead_in_ms"),
    "trailingSilenceMs": ("timing", "trailing_silence_ms"),
    "minUtteranceMs": ("timing", "min_utterance_ms"),
    "maxUtteranceMs": ("timing", "max_utterance_ms"),
    "model": ("stt", "model"),
    "micThresholdDb": ("audio", "mic_threshold_db"),
    "echoTailMs": ("audio", "echo_tail_ms"),
    "refractoryMs": ("wake", "refractory_ms"),
    "wakeConfidence": ("audio", "wake_confidence"),
    "livePartials": ("stt", "live_partials"),
    "bargeIn": ("audio", "barge_in"),
    "bargeFactor": ("audio", "barge_factor"),
    "permissions": ("agent", "permissions"),
    "projectDir": ("agent", "project_dir"),
    "endpointUrl": ("agent", "endpoint_url"),
    "endpointModel": ("agent", "endpoint_model"),
    "endpointIsAgent": ("agent", "endpoint_is_agent"),
    "conversationMode": ("wake", "conversation_mode"),
    "followUpMs": ("timing", "follow_up_ms"),
}


class Config:
    """Reloaded between utterances so knobs can be turned mid-session."""

    def __init__(self) -> None:
        self._stamps: tuple = ()
        self._values: dict = dict(DEFAULTS)
        self.source = "defaults"
        self.reload()

    @staticmethod
    def _stamp(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _from_shell_json(self) -> dict:
        try:
            data = json.loads(SHELL_JSON.read_text())
        except Exception:
            return {}
        layout = (data.get("bar") or {}).get("layout") or {}
        for entries in layout.values():
            for entry in entries or []:
                if entry.get("id") == PLUGIN_ID:
                    return {k: v for k, v in entry.items() if k != "id"}
        return {}

    def _from_toml(self) -> dict:
        try:
            raw = tomllib.loads(config_toml().read_text())
        except Exception:
            return {}
        out = {}
        for key, (section, name) in TOML_ALIASES.items():
            value = (raw.get(section) or {}).get(name)
            if value is not None:
                out[key] = value
        return out

    def reload(self) -> bool:
        """True if anything on disk changed since the last read."""
        stamps = (self._stamp(SHELL_JSON), self._stamp(config_toml()))
        if stamps == self._stamps:
            return False
        self._stamps = stamps

        merged = dict(DEFAULTS)
        merged.update(self._from_toml())
        widget = self._from_shell_json()
        merged.update(widget)
        changed = merged != self._values
        self._values = merged
        self.source = f"shell.json[{PLUGIN_ID}]" if widget else (
            "agentvoice.toml" if config_toml().exists() else "defaults")
        return changed

    def __getitem__(self, key: str):
        # The bar schema has no float type, so confidence travels as a whole
        # percent and is converted here rather than everywhere it is read.
        if key == "wakeConfidence" and "wakeConfidencePct" in self._values:
            return float(self._values["wakeConfidencePct"]) / 100.0
        return self._values.get(key, DEFAULTS.get(key))

    def int(self, key: str) -> int:
        return int(self[key])

    def bool(self, key: str) -> bool:
        return bool(self[key])

    def str(self, key: str) -> str:
        return str(self[key])

    def level_for(self, agent: str | None) -> str:
        """The permission level chosen for this agent, defaulting to "ask".

        Levels are per agent because trust is a judgement about one program's
        capabilities, and those differ enormously: Claude Code can be stopped
        mid-call by a hook we answer, Codex cannot be stopped at all. A single
        global level meant that trusting Claude Code silently handed the same
        trust to whatever `omarchy default agent` was switched to next.

        Anything unrecognised reads as "ask". An agent nobody has made a
        decision about has not been trusted, and the absence of a decision
        must never be read as a permissive one.
        """
        raw = self["permissions"]
        if not isinstance(raw, dict) or not agent:
            return "ask"
        value = raw.get(agent)
        return value if value in ("ask", "edits", "trusted") else "ask"


class StateFile:
    """What the daemon is doing, written where the bar widget can watch it.

    Same contract shape Voxtype uses for its own indicator: one small file,
    rewritten on every transition. Written to a temp file and renamed so a
    watcher never reads a half-written line.
    """

    def __init__(self) -> None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        self.path = RUNTIME_DIR / "state"
        self._last: dict = {}
        # Facts that hold for the whole session and belong in every payload,
        # so the panel can render what the running agent actually supports
        # instead of keeping its own copy of that knowledge and drifting.
        self._context: dict = {}

    def describe(self, **fields) -> None:
        """Set the sticky fields merged into every subsequent publish."""
        if fields != self._context:
            self._context = dict(fields)
            if self._last:
                self.publish(self._last.get("state", "off"),
                             **{k: v for k, v in self._last.items()
                                if k != "state" and k not in self._context})

    def publish(self, state: str, **extra) -> None:
        payload = {"state": state, **self._context, **extra}
        if payload == self._last:
            return
        self._last = payload
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload))
        except FileNotFoundError:
            # The state file lives in /run, which is tmpfs, is cleanable, and
            # is deleted outright by this project's own uninstaller. The
            # directory was created once in __init__, so when it went the next
            # publish raised FileNotFoundError out of the run loop and killed
            # the daemon -- twice, in one session. Losing the readout the bar
            # watches is a cosmetic failure; it must not be a fatal one.
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)

    @property
    def current(self) -> str:
        """The last state published, for code that must not act out of turn."""
        return str(self._last.get("state", "off"))

    def clear(self) -> None:
        """Best effort: this runs on the way out, including while unwinding a
        failure. It raised the same error as the exception it was cleaning up
        after, which replaced the traceback that said what really happened."""
        try:
            self.publish("off")
        except OSError:
            pass


class Speaker:
    """Piper, loaded once and reused. 22ms to first audio, 22x realtime.

    Splits on sentences before synthesising, because Piper returns a whole
    utterance before yielding its first chunk -- so a long reply would
    otherwise sit silent while the entire thing renders.
    """

    #: Set when the requested voice was not on disk and another was used
    #: instead. The caller prints it, because silence with the reason in a log
    #: is the failure this exists to avoid.
    substituted_for: str = ""

    def __init__(self, voice: str = "lessac-medium") -> None:
        from piper import PiperVoice
        voice, substituted = self._resolve(voice)
        path = piper_voices() / f"en_US-{voice}.onnx"
        self._voice = PiperVoice.load(str(path))
        self._rate = getattr(self._voice.config, "sample_rate", 22050)
        self.name = voice
        self.substituted_for = substituted
        #: What the settings asked for, which is what a later reload compares
        #: against. Comparing the loaded name would see a substitution as a
        #: changed setting and reload Piper on every reload.
        self.requested = substituted or voice
        self._stop = threading.Event()

    def superseded(self) -> bool:
        """True when the voice originally asked for has since been downloaded.

        Anything a model loads is reconciled rather than constructed once, and
        a voice file can appear underneath a running daemon -- install.sh
        fetches it. Without this, a substitution outlived the download and the
        only cure was changing an unrelated setting to force a rebuild.
        """
        if not self.substituted_for:
            return False
        return (piper_voices() / f"en_US-{self.substituted_for}.onnx").exists()

    @staticmethod
    def _resolve(voice: str) -> tuple[str, str]:
        """The requested voice, or any other one on disk.

        The settings list every voice the project knows, marking the ones not
        downloaded -- and selecting one used to raise here, leaving the daemon
        with no speaker at all. Nothing then retried until some unrelated
        setting changed, so the only cure people found was switching the wake
        engine and back. Falling back keeps speech working and names the
        substitution; being wrong out loud beats being silent.
        """
        here = piper_voices()
        if (here / f"en_US-{voice}.onnx").exists():
            return voice, ""
        available = sorted(p.stem.removeprefix("en_US-")
                           for p in here.glob("en_US-*.onnx"))
        if not available:
            raise FileNotFoundError(f"no piper voice at {here}")
        # Prefer the default when it is one of them: it is the voice every
        # install has, so the substitution is the least surprising one.
        pick = "lessac-medium" if "lessac-medium" in available else available[0]
        return pick, voice

    @staticmethod
    def sentences(text: str) -> list[str]:
        import re
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p for p in parts if p]

    def cancel(self) -> None:
        self._stop.set()

    def say(self, text: str, watch=None) -> float:
        """Speak, sentence by sentence. Returns seconds of audio produced.

        `watch` is called between audio chunks and stops the speech when it
        returns true -- which is how barge-in listens while we are talking.
        Called here rather than from a thread on purpose: this opens a
        PortAudio output stream while an input stream is already running, and
        driving two of those from two threads is what crashed every benchmark
        script that tried it (libasound, use-after-free, SEGV). One thread,
        one interleaved loop, no shared PCM handles.
        """
        import numpy as np
        import sounddevice as sd

        self._stop.clear()
        total = 0.0
        with sd.OutputStream(samplerate=self._rate, channels=1,
                             dtype="int16") as out:
            for sentence in self.sentences(text):
                if self._stop.is_set():
                    break
                for chunk in self._voice.synthesize(sentence):
                    if self._stop.is_set():
                        break
                    pcm = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
                    out.write(pcm)
                    total += len(pcm) / self._rate
                    if watch is not None and watch():
                        self._stop.set()
                        break
        return total
