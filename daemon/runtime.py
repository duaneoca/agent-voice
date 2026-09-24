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
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)

    @property
    def current(self) -> str:
        """The last state published, for code that must not act out of turn."""
        return str(self._last.get("state", "off"))

    def clear(self) -> None:
        self.publish("off")


class Speaker:
    """Piper, loaded once and reused. 22ms to first audio, 22x realtime.

    Splits on sentences before synthesising, because Piper returns a whole
    utterance before yielding its first chunk -- so a long reply would
    otherwise sit silent while the entire thing renders.
    """

    def __init__(self, voice: str = "lessac-medium") -> None:
        from piper import PiperVoice
        path = piper_voices() / f"en_US-{voice}.onnx"
        if not path.exists():
            raise FileNotFoundError(f"no piper voice at {path}")
        self._voice = PiperVoice.load(str(path))
        self._rate = getattr(self._voice.config, "sample_rate", 22050)
        self.name = voice
        self._stop = threading.Event()

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
