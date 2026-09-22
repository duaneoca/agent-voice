#!/usr/bin/env python3
"""Benchmark local TTS on this machine.

Two numbers decide whether a voice feels alive:
  first_audio_ms - how long after the agent decides to speak before sound
                   starts. This is what the user actually perceives.
  rtf            - synthesis time over audio duration. Must stay under 1.0 or
                   sentence-by-sentence streaming falls behind the speech.

    python bench/tts_bench.py
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
import wave
from pathlib import Path

BENCH = Path(__file__).resolve().parent
MODELS = BENCH / "models"
RESULTS = BENCH / "results"

# What an agent actually says back, not what you say to it.
REPLIES = [
    "Done.",
    "That function returns None when the list is empty.",
    "I found three places where the retry loop can spin forever. "
    "The first is in the connection handler.",
    "Before I change anything, I should tell you that the test suite is "
    "currently failing on main, so we will not be able to tell whether my "
    "edit broke something or whether it was already broken.",
]


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


class EspeakEngine:
    name = "espeak-ng"

    def __init__(self):
        if not shutil.which("espeak-ng"):
            raise RuntimeError("espeak-ng not installed")

    def synth(self, text: str, out: Path) -> tuple[float, float]:
        t0 = time.perf_counter()
        subprocess.run(["espeak-ng", "-w", str(out), text],
                       check=True, capture_output=True)
        total = (time.perf_counter() - t0) * 1000
        return total, total  # too fast to stream; first audio is the whole run


class PiperEngine:
    def __init__(self, onnx: Path):
        from piper import PiperVoice
        self.name = f"piper-{onnx.stem.replace('en_US-', '')}"
        self._voice = PiperVoice.load(str(onnx))

    def synth(self, text: str, out: Path) -> tuple[float, float]:
        t0 = time.perf_counter()
        first = None
        chunks = []
        for chunk in self._voice.synthesize(text):
            if first is None:
                first = (time.perf_counter() - t0) * 1000
            chunks.append(chunk)
        total = (time.perf_counter() - t0) * 1000

        raw = b"".join(getattr(c, "audio_int16_bytes", c) for c in chunks)
        sample_rate = getattr(self._voice.config, "sample_rate", 22050)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(raw)
        return first if first is not None else total, total


def build_engines():
    try:
        yield EspeakEngine()
    except Exception as e:
        print(f"  ! espeak-ng: {e} — skipping")
    for onnx in sorted((MODELS / "piper").glob("*.onnx")) if (MODELS / "piper").is_dir() else []:
        try:
            yield PiperEngine(onnx)
        except Exception as e:
            print(f"  ! {onnx.name}: {type(e).__name__}: {e} — skipping")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="repeats per line; best is kept")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    tmp = RESULTS / "tts_audio"
    tmp.mkdir(exist_ok=True)
    out: dict[str, dict] = {}

    for eng in build_engines():
        print(f"  {eng.name} ...", end="", flush=True)
        per_line = []
        for i, text in enumerate(REPLIES):
            wav = tmp / f"{eng.name}-{i}.wav"
            best = None
            for _ in range(args.runs):  # first run pays cold caches
                first_ms, total_ms = eng.synth(text, wav)
                if best is None or total_ms < best[1]:
                    best = (first_ms, total_ms)
            dur = wav_duration(wav)
            per_line.append({"chars": len(text), "audio_s": round(dur, 2),
                             "first_audio_ms": round(best[0], 1),
                             "total_ms": round(best[1], 1),
                             "rtf": round(best[1] / 1000 / dur, 3) if dur else None})
        out[eng.name] = {
            "median_first_audio_ms": round(
                sorted(p["first_audio_ms"] for p in per_line)[len(per_line) // 2], 1),
            "mean_rtf": round(sum(p["rtf"] for p in per_line) / len(per_line), 3),
            "lines": per_line,
        }
        print(f" first audio {out[eng.name]['median_first_audio_ms']:.0f}ms  "
              f"RTF {out[eng.name]['mean_rtf']:.2f}")

    (RESULTS / "tts.json").write_text(json.dumps(out, indent=2))
    print(f"\n  -> {RESULTS/'tts.json'}   (audio in {tmp}/)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
