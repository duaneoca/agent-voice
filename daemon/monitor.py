#!/usr/bin/env python3
"""Watch the wake word decide, live.

A wake that fires leaves a line in the journal. A wake that does not leaves
nothing at all, so "it is inconsistent" is invisible from the logs -- the only
thing recorded is the successes. This prints what the detector actually sees,
frame by frame, so a missed wake can be attributed rather than guessed at.

It reads the same config the daemon does and builds the same engine, so what
you calibrate here is what runs. It can be left running alongside the daemon:
PipeWire allows several readers on one microphone.

    agentvoice monitor              # follow the configured engine
    agentvoice monitor --seconds 60
"""
from __future__ import annotations

import argparse
import queue
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime import Config  # noqa: E402
from wake_listen import CHUNK, RATE, OwwWake, frame_db  # noqa: E402

DIM, RED, GRN, YEL, CYA, BLD, OFF = (
    "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[1m", "\033[0m")


def bar(value: float, width: int = 24) -> str:
    filled = max(0, min(width, int(round(value * width))))
    return "█" * filled + "·" * (width - filled)


def main() -> int:
    import sounddevice as sd

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = until ctrl-c")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--no-verifier", action="store_true",
                    help="score with the base model only, ignoring any trained verifier")
    ap.add_argument("--all-frames", action="store_true",
                    help="show quiet frames too, not just ones past the gate")
    args = ap.parse_args()

    cfg = Config()
    engine = cfg.str("engine")
    gate = float(cfg["micThresholdDb"])

    if engine != "openwakeword":
        print(f"  {YEL}The configured engine is {engine}, which scores by grammar "
              f"rather than by confidence.{OFF}")
        print(f"  {DIM}This monitor only has numbers to show for openWakeWord. "
              f"Switch engines in settings to use it.{OFF}")
        return 1

    threshold = cfg.int("owwThresholdPct") / 100.0
    oww = OwwWake(cfg.str("owwModel"), threshold,
                  use_verifier=not args.no_verifier)

    print(f"\n  {BLD}{oww.key}{OFF}  threshold {threshold:.2f}  "
          f"gate {gate:.0f} dBFS  "
          f"{'verifier loaded' if oww.verifier else 'no verifier'}")
    print(f"  {DIM}Say the wake phrase a few times. Quiet frames are hidden "
          f"unless --all-frames.{OFF}\n")

    frames: queue.Queue[bytes] = queue.Queue()

    def cb(indata, _f, _t, status):
        frames.put(bytes(indata))

    started = time.time()
    peak_since_speech = 0.0
    speaking = False
    fires = 0
    near_misses = 0

    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                           blocksize=CHUNK, device=args.device, callback=cb):
        try:
            while args.seconds <= 0 or time.time() - started < args.seconds:
                pcm = frames.get()
                level = frame_db(pcm)
                gated = level < gate

                # The daemon drops sub-gate frames before the detector sees
                # them, so the monitor does the same -- otherwise it would be
                # measuring a different pipeline from the one that runs.
                # Feed exactly as the daemon does -- same rebuffering from
                # 100ms capture frames to openWakeWord's 80ms ones -- and read
                # back the score it computed. Scoring a truncated frame here
                # would measure a different pipeline from the one that runs.
                score = 0.0
                if not gated:
                    oww.feed(pcm)
                    score = oww.last_score

                if not gated or args.all_frames:
                    mark = ""
                    colour = DIM
                    if score >= threshold:
                        mark, colour, fires = "  ← WOULD FIRE", GRN, fires + 1
                    elif score >= threshold * 0.6:
                        mark, colour = "  ← close", YEL
                        near_misses += 1
                    print(f"  {colour}{level:6.1f} dBFS  {bar(score)} "
                          f"{score:.3f}{mark}{OFF}"
                          + (f"  {DIM}(below gate){OFF}" if gated else ""))

                if level >= gate:
                    peak_since_speech = max(peak_since_speech, score)
                    speaking = True
                elif speaking:
                    print(f"  {CYA}— utterance peak {peak_since_speech:.3f} "
                          f"({'fires' if peak_since_speech >= threshold else 'MISSED'}"
                          f" at {threshold:.2f}){OFF}\n")
                    peak_since_speech, speaking = 0.0, False
        except KeyboardInterrupt:
            pass

    print(f"\n  {BLD}{fires}{OFF} frames over threshold, "
          f"{near_misses} close.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
