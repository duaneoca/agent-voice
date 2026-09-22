#!/usr/bin/env python3
"""Record the benchmark corpus in your own voice, on your own mic.

Published WER numbers tell you nothing about this microphone, this room, or
this vocabulary. Two minutes of reading gives us the only data that answers
the actual question.

    python bench/record.py              # record whatever is still missing
    python bench/record.py --redo 09    # re-record one prompt
    python bench/record.py --list-devices
"""
from __future__ import annotations

import argparse
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

RATE = 16_000          # what both Vosk and Whisper want natively
CHANNELS = 1
BENCH = Path(__file__).resolve().parent
CORPUS = BENCH / "corpus"
WAVDIR = CORPUS / "wav"


def load_prompts() -> list[tuple[str, str, str]]:
    out = []
    for line in (CORPUS / "prompts.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        pid, category, text = line.split("|", 2)
        out.append((pid, category, text))
    return out


def check_level(path: Path) -> str | None:
    """Clipped audio cannot be repaired, and benchmarking on it measures the
    input gain rather than the recogniser. Catch it while the user is still
    sitting here."""
    with wave.open(str(path), "rb") as w:
        pcm = w.readframes(w.getnframes())
    if not pcm:
        return "no audio captured"
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    peak = np.abs(x).max() / 32768
    rms = float(np.sqrt((x ** 2).mean())) / 32768
    clipped = float((np.abs(x) >= 32700).mean())
    if clipped > 0.001:
        return (f"CLIPPING ({clipped:.1%} of samples) — lower the mic gain:\n"
                f"       amixer -c 0 sset Capture 49")
    if rms < 0.004:
        return "very quiet — move closer or raise the gain"
    if peak < 0.05:
        return "almost silent — did the mic pick you up?"
    return None


def record_one(path: Path, device: int | None) -> float:
    """Record until the user presses Enter. Returns duration in seconds."""
    chunks: list[bytes] = []
    stop = threading.Event()

    def callback(indata, _frames, _time, status):
        if status:
            print(f"  [audio: {status}]", file=sys.stderr)
        chunks.append(bytes(indata))

    def wait_for_enter():
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        stop.set()

    with sd.RawInputStream(samplerate=RATE, channels=CHANNELS, dtype="int16",
                           device=device, callback=callback):
        threading.Thread(target=wait_for_enter, daemon=True).start()
        while not stop.is_set():
            sd.sleep(50)

    data = b"".join(chunks)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)
    return len(data) / (2 * CHANNELS * RATE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--redo", nargs="*", metavar="ID")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    WAVDIR.mkdir(parents=True, exist_ok=True)
    prompts = load_prompts()

    if args.redo:
        todo = [p for p in prompts if p[0] in set(args.redo)]
    else:
        todo = [p for p in prompts if not (WAVDIR / f"{p[0]}.wav").exists()]

    if not todo:
        print("Nothing to record. Use --redo <id> to replace one.")
        return 0

    print(f"\n  {len(todo)} to record. Enter starts, Enter stops. Ctrl-C quits.")
    print("  Speak normally — the way you'd actually talk to the agent.\n")

    for i, (pid, category, text) in enumerate(todo, 1):
        print(f"  [{i}/{len(todo)}]  ({category})")
        print(f"  \033[1m{text}\033[0m")
        try:
            input("  Enter to start... ")
        except (KeyboardInterrupt, EOFError):
            print("\n  Stopped.")
            break
        print("  \033[31m● recording\033[0m — Enter to stop.", end="", flush=True)
        try:
            dur = record_one(WAVDIR / f"{pid}.wav", args.device)
        except KeyboardInterrupt:
            print("\n  Stopped.")
            break
        warning = check_level(WAVDIR / f"{pid}.wav")
        if warning:
            print(f"\r  \033[33m! {pid}.wav ({dur:.1f}s) — {warning}\033[0m")
            print(f"    re-record with:  python bench/record.py --redo {pid}\n")
        else:
            print(f"\r  saved {pid}.wav ({dur:.1f}s){' ' * 20}\n")

    have = sorted(p for p in prompts if (WAVDIR / f"{p[0]}.wav").exists())
    with (CORPUS / "ground_truth.tsv").open("w") as f:
        f.write("id\tcategory\ttext\n")
        for pid, category, text in have:
            f.write(f"{pid}\t{category}\t{text}\n")
    print(f"  Corpus: {len(have)}/{len(prompts)} recorded -> {CORPUS/'ground_truth.tsv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
