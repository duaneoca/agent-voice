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

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ID = "duaneoca.agentvoice"
SHELL_JSON = Path.home() / ".config/omarchy/shell.json"
LOCAL_TOML = ROOT / "prototype/agentvoice.toml"

RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")) / "agentvoice"

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
    "wakeConfidence": 0.70,
    "model": "tiny.en",
    "speakReplies": True,
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
            raw = tomllib.loads(LOCAL_TOML.read_text())
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
        stamps = (self._stamp(SHELL_JSON), self._stamp(LOCAL_TOML))
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
            "agentvoice.toml" if LOCAL_TOML.exists() else "defaults")
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

    def publish(self, state: str, **extra) -> None:
        payload = {"state": state, **extra}
        if payload == self._last:
            return
        self._last = payload
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)

    def clear(self) -> None:
        self.publish("off")


class Speaker:
    """Piper, loaded once and reused. 22ms to first audio, 22x realtime.

    Splits on sentences before synthesising, because Piper returns a whole
    utterance before yielding its first chunk -- so a long reply would
    otherwise sit silent while the entire thing renders.
    """

    VOICES = ROOT / "bench/models/piper"

    def __init__(self, voice: str = "lessac-medium") -> None:
        from piper import PiperVoice
        path = self.VOICES / f"en_US-{voice}.onnx"
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

    def say(self, text: str) -> float:
        """Speak, sentence by sentence. Returns seconds of audio produced."""
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
        return total
