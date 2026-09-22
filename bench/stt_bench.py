#!/usr/bin/env python3
"""Benchmark local STT engines on this machine's CPU, voice, and microphone.

Reports the number that actually matters for a voice assistant: how long after
you stop talking before the text exists. Vosk streams, so most of its work
happens while you are still speaking; Whisper cannot start until you stop.
Comparing raw wall-clock would flatter Whisper unfairly, so both are measured.

    python bench/stt_bench.py
    python bench/stt_bench.py --engines vosk-small whisper-base.en
"""
from __future__ import annotations

import argparse
import json
import re
import resource
import time
import wave
from pathlib import Path

BENCH = Path(__file__).resolve().parent
CORPUS = BENCH / "corpus"
MODELS = BENCH / "models"
RESULTS = BENCH / "results"

WHISPER_SIZES = ["tiny.en", "base.en", "small.en"]


# ---------------------------------------------------------------- scoring

_PUNCT = re.compile(r"[^\w\s]")


def normalize(text: str) -> list[str]:
    """Lowercase, drop punctuation. Both engines get the same treatment, and
    Vosk's small models emit no punctuation at all, so scoring it as an error
    would measure the wrong thing."""
    return _PUNCT.sub(" ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> tuple[float, int, int]:
    """Word error rate by Levenshtein distance. Returns (rate, edits, ref_len)."""
    r, h = normalize(reference), normalize(hypothesis)
    if not r:
        return (0.0 if not h else 1.0), len(h), 0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[-1] / len(r), prev[-1], len(r)


def read_wav(path: Path) -> tuple[bytes, float]:
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == 16_000 and w.getnchannels() == 1, f"{path}: want 16k mono"
        data = w.readframes(w.getnframes())
    return data, len(data) / 32_000.0


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# ---------------------------------------------------------------- engines


class VoskEngine:
    """Streaming. Fed in 100ms chunks the way a live daemon would."""

    CHUNK = 3200  # 100ms of 16k mono int16

    def __init__(self, model_dir: Path, threads: int):
        import vosk
        vosk.SetLogLevel(-1)
        self.name = f"vosk-{model_dir.name.replace('vosk-model-', '')}"
        self._vosk = vosk
        self._model = vosk.Model(str(model_dir))

    def transcribe(self, pcm: bytes) -> tuple[str, float, float]:
        import json as _json
        rec = self._vosk.KaldiRecognizer(self._model, 16_000)
        t0 = time.perf_counter()
        # Stream everything but the tail, as a live daemon would during speech.
        for off in range(0, max(0, len(pcm) - self.CHUNK), self.CHUNK):
            rec.AcceptWaveform(pcm[off:off + self.CHUNK])
        streamed = time.perf_counter()
        # The tail plus finalization is all the user waits for after speaking.
        tail = pcm[max(0, len(pcm) - self.CHUNK):]
        rec.AcceptWaveform(tail)
        text = _json.loads(rec.FinalResult()).get("text", "")
        t1 = time.perf_counter()
        return text, (t1 - t0) * 1000, (t1 - streamed) * 1000


class WhisperEngine:
    """Batch. Nothing can start until the utterance is complete."""

    def __init__(self, size: str, threads: int, vocab: str | None = None):
        from faster_whisper import WhisperModel
        self.name = f"whisper-{size}" + ("+vocab" if vocab else "")
        self._prompt = vocab
        self._model = WhisperModel(size, device="cpu", compute_type="int8",
                                   cpu_threads=threads)

    def transcribe(self, pcm: bytes) -> tuple[str, float, float]:
        import numpy as np
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        segments, _ = self._model.transcribe(audio, language="en", beam_size=1,
                                             initial_prompt=self._prompt)
        text = " ".join(s.text for s in segments).strip()
        total = (time.perf_counter() - t0) * 1000
        return text, total, total  # batch: perceived latency IS the whole run


# ---------------------------------------------------------------- driver


def load_vocab(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    terms = [l.strip() for l in path.read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    return ", ".join(terms) + "." if terms else None


def build_engines(names: list[str], threads: int, vocab: str | None = None):
    for n in names:
        try:
            if n.startswith("vosk-"):
                suffix = n[len("vosk-"):]
                matches = sorted(MODELS.glob(f"vosk-model-{suffix}*"))
                if not matches:
                    print(f"  ! {n}: no model in {MODELS} — skipping")
                    continue
                yield VoskEngine(matches[0], threads)
            else:
                yield WhisperEngine(n[len("whisper-"):], threads, vocab)
        except Exception as e:  # a missing engine must not sink the whole run
            print(f"  ! {n}: {type(e).__name__}: {e} — skipping")


def main() -> int:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--engines", nargs="*", default=None)
    ap.add_argument("--corpus", type=Path, default=CORPUS,
                    help="corpus dir holding ground_truth.tsv and wav/")
    ap.add_argument("--vocab", type=Path, default=None,
                    help="term list fed to Whisper as initial_prompt")
    ap.add_argument("--threads", type=int, default=(os.cpu_count() or 4) // 2,
                    help="default: physical cores")
    args = ap.parse_args()

    corpus = args.corpus
    gt_path = corpus / "ground_truth.tsv"
    if not gt_path.exists():
        print("No corpus yet. Run:  python bench/record.py")
        return 1

    rows = [l.split("\t") for l in gt_path.read_text().splitlines()[1:] if l.strip()]
    clips = [(pid, cat, text, *read_wav(corpus / "wav" / f"{pid}.wav")) for pid, cat, text in rows]
    audio_total = sum(c[4] for c in clips)
    print(f"\n  Corpus: {len(clips)} clips, {audio_total:.1f}s of audio")
    print(f"  Threads: {args.threads}\n")

    names = args.engines or (["vosk-small-en-us"] + [f"whisper-{s}" for s in WHISPER_SIZES])
    RESULTS.mkdir(exist_ok=True)
    out: dict[str, dict] = {}

    for eng in build_engines(names, args.threads, load_vocab(args.vocab)):
        print(f"  {eng.name} ...", end="", flush=True)
        rss_before = peak_rss_mb()
        per_clip, edits, ref_words = [], 0, 0
        t_start = time.perf_counter()
        for pid, cat, ref, pcm, dur in clips:
            hyp, total_ms, perceived_ms = eng.transcribe(pcm)
            rate, e, n = wer(ref, hyp)
            edits += e
            ref_words += n
            per_clip.append({"id": pid, "category": cat, "ref": ref, "hyp": hyp,
                             "wer": round(rate, 4), "audio_s": round(dur, 2),
                             "total_ms": round(total_ms, 1),
                             "perceived_ms": round(perceived_ms, 1),
                             "rtf": round(total_ms / 1000 / dur, 3) if dur else None})
        wall = time.perf_counter() - t_start
        by_cat: dict[str, list[float]] = {}
        for c in per_clip:
            by_cat.setdefault(c["category"], []).append(c["wer"])
        out[eng.name] = {
            "wer": round(edits / ref_words, 4) if ref_words else None,
            "wer_by_category": {k: round(sum(v) / len(v), 4) for k, v in sorted(by_cat.items())},
            "median_perceived_ms": round(sorted(c["perceived_ms"] for c in per_clip)[len(per_clip) // 2], 1),
            "p90_perceived_ms": round(sorted(c["perceived_ms"] for c in per_clip)[int(len(per_clip) * 0.9)], 1),
            "mean_rtf": round(sum(c["rtf"] for c in per_clip) / len(per_clip), 3),
            "peak_rss_mb": round(peak_rss_mb() - rss_before, 1),
            "wall_s": round(wall, 1),
            "clips": per_clip,
        }
        print(f" WER {out[eng.name]['wer']:.1%}  "
              f"median {out[eng.name]['median_perceived_ms']:.0f}ms  "
              f"RTF {out[eng.name]['mean_rtf']:.2f}")

    (RESULTS / f"stt-{corpus.name}{'-vocab' if args.vocab else ''}.json").write_text(json.dumps(out, indent=2))
    print(f"\n  -> {RESULTS/f'stt-{corpus.name}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
