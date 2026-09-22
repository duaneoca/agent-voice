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


def run_ab(cfg, threshold, with_verifier, seconds: float = 0.0) -> int:
    """Score the same audio with and without the verifier, utterance by
    utterance, and report whether it helped, hurt, or did nothing."""
    import sounddevice as sd

    if not with_verifier.verifier:
        print(f"  {YEL}No verifier is trained for {with_verifier.key}, so there "
              f"is nothing to compare against.{OFF}")
        return 1

    without = OwwWake(cfg.str("owwModel"), threshold, use_verifier=False)

    print(f"\n  {BLD}{with_verifier.key}{OFF}  threshold {threshold:.2f}")
    print(f"  {DIM}Both detectors see identical audio. Say the wake phrase "
          f"ten times or so, then ctrl-c.{OFF}\n")
    print(f"  {'utterance':>9s}  {'with verifier':>14s}  {'base model':>11s}   verdict")

    frames: queue.Queue[bytes] = queue.Queue()
    sd.default.dtype = "int16"

    rows = []
    speaking = False
    peak_a = peak_b = 0.0
    quiet = 0

    def cb(indata, _f, _t, status):
        frames.put(bytes(indata))

    with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                           blocksize=CHUNK, callback=cb):
        started = time.time()
        try:
            while seconds <= 0 or time.time() - started < seconds:
                pcm = frames.get()
                with_verifier.feed(pcm)
                without.feed(pcm)
                a, b = with_verifier.last_score, without.last_score
                peak_a, peak_b = max(peak_a, a), max(peak_b, b)

                if max(a, b) > 0.02:
                    speaking, quiet = True, 0
                elif speaking:
                    quiet += 1
                    if quiet >= 8:            # ~800ms of nothing ends it
                        n = len(rows) + 1
                        fa, fb = peak_a >= threshold, peak_b >= threshold
                        if fa and not fb:   verdict, col = "verifier SAVED it", GRN
                        elif fb and not fa: verdict, col = "verifier BLOCKED it", RED
                        elif fa and fb:     verdict, col = "both fired", DIM
                        else:               verdict, col = "both missed", YEL
                        print(f"  {col}{n:9d}  {peak_a:14.3f}  {peak_b:11.3f}   {verdict}{OFF}")
                        rows.append((peak_a, peak_b))
                        speaking, peak_a, peak_b, quiet = False, 0.0, 0.0, 0
        except KeyboardInterrupt:
            pass

    if not rows:
        print(f"\n  {YEL}Nothing was heard.{OFF}")
        return 1

    fa = sum(1 for a, _ in rows if a >= threshold)
    fb = sum(1 for _, b in rows if b >= threshold)
    mean_a = sum(a for a, _ in rows) / len(rows)
    mean_b = sum(b for _, b in rows) / len(rows)
    print(f"\n  {BLD}{len(rows)} utterances{OFF}")
    print(f"    with verifier   fired {fa:2d}/{len(rows)}   mean peak {mean_a:.3f}")
    print(f"    base model      fired {fb:2d}/{len(rows)}   mean peak {mean_b:.3f}")
    if fa > fb:
        print(f"\n  {GRN}The verifier is helping.{OFF}")
    elif fb > fa:
        print(f"\n  {RED}The verifier is costing you {fb - fa} wake(s). "
              f"Delete it to fall back to the base model:{OFF}")
        print(f"    rm ~/.local/share/agentvoice/verifiers/{with_verifier.key}.joblib")
    else:
        print(f"\n  {DIM}No difference in what fires. Compare the mean peaks: "
              f"a lower mean with the verifier means less headroom.{OFF}")
    print(f"  {DIM}This only measures YOUR voice. What a verifier is for -- "
          f"rejecting other people -- needs someone else at the mic.{OFF}\n")
    return 0


def main() -> int:
    import sounddevice as sd

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = until ctrl-c")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--no-verifier", action="store_true",
                    help="score with the base model only, ignoring any trained verifier")
    ap.add_argument("--ab", action="store_true",
                    help="score with AND without the verifier on the same audio")
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

    # Running both detectors over the same frames is the only way to compare
    # them honestly: saying the phrase twenty times into one build and twenty
    # times into another measures the difference between two performances at
    # least as much as the difference between two models.
    if args.ab:
        return run_ab(cfg, threshold, oww, args.seconds)

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
                # Every frame, gate or no gate -- the daemon no longer gates
                # this path, and the monitor has to match it or it measures a
                # pipeline that does not run.
                oww.feed(pcm)
                score = oww.last_score

                if score > 0.001 or not gated or args.all_frames:
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
