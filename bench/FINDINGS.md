# Benchmark findings

Measured 2026-09-21 on the development machine: MacBookPro11,5 (2014),
i7-4980HQ, 4 cores / 8 threads, AVX2 without AVX-512, 16GB, no usable ML GPU.
Corpus: 17 clips, 98s, read aloud by the project author into the built-in
laptop microphone.

These numbers describe *this* machine. Re-run `bench/` anywhere else before
trusting them.

## 1. Custom vocabulary beats model size, decisively

A fourteen-word `initial_prompt` outperforms a six-times-larger model at a
sixth of the latency.

| config | overall WER | domain WER | perceived latency |
|---|---|---|---|
| whisper tiny.en | 9.8% | 27.2% | 317ms |
| **whisper tiny.en + vocab** | **4.9%** | **9.9%** | **358ms** |
| whisper small.en, no vocab | 7.8% | 18.6% | 2165ms |

Confirmed independently through Voxtype's whisper.cpp (f16 rather than
faster-whisper's int8, so absolute numbers differ, conclusion does not):
tiny.en 11.0% -> 6.5% with the prompt; base.en + vocab lands at 6.9%.

Custom vocabulary is therefore a first-class component, not a tuning detail.
It ships as `daemon/vocab.txt`, overridable at `~/.config/agentvoice/vocab.txt`.

## 2. Ordinary speech is already solved; jargon is the whole problem

WER by category, no vocabulary prompt:

| engine | short | plain | domain | long |
|---|---|---|---|---|
| vosk-small | 10.0% | 2.5% | **39.2%** | 10.5% |
| whisper tiny.en | 0.0% | 0.0% | **28.2%** | 2.6% |
| whisper base.en | 0.0% | 1.2% | **25.1%** | 2.6% |
| whisper small.en | 0.0% | 2.6% | **18.6%** | 2.6% |

tiny.en scores 0.0% on conversational speech. Every remaining error is a
proper noun. No engine at any size transcribed `kubectl` correctly:
"cook controlled", "coupe control", "koop control", "Koopkertrol".

## 3. Rank on perceived latency, not real-time factor

Vosk consumes audio while you are still speaking, so only the tail remains
when you stop. Whisper cannot start until the utterance ends, so its whole
run is dead air. whisper-tiny has the **better** RTF (0.13 vs 0.23) and **ten
times** the perceived latency (323ms vs 30ms). Published RTF rankings pick the
wrong engine for this workload.

Voxtype's `whisper.eager_processing` ("transcribe overlapping chunks while you
are still speaking") is the mitigation and is untested here.

## 4. TTS has margin to spare

| engine | short reply | 190-char paragraph | RTF |
|---|---|---|---|
| espeak-ng | 6ms | 15ms | 0.002 |
| piper lessac-low | 18ms | 332ms | 0.034 |
| **piper lessac-medium** | **22ms** | **438ms** | **0.045** |

22x faster than realtime. Piper returns `first_audio == total` on most lines,
meaning it synthesises a whole utterance before yielding — so sentence
splitting must happen in the daemon, not be delegated to Piper.

## 5. Two hardware realities

**Thermal throttling.** `package_throttle_count` reached 13,471 during these
runs. Back-to-back sweeps pushed small.en from 2165ms to 6283ms while WER held
constant. Sustained inference degrades this laptop 2-3x, so a long voice
session gets slower. Budget for it.

**Microphone gain.** ALSA `Capture` shipped at +12dB, clipping 1.46% of samples
at a normal speaking distance. Accuracy survived it, but install-time gain
calibration belongs in the product. `record.py` now refuses to accept a clipped
clip silently.

## 6. Packaging

The whole stack imports into one process on Arch's system Python 3.14:
pacman's `python-vosk` alongside wheel-installed `faster-whisper` and
`piper-tts`, via a venv built with `--system-site-packages`. onnxruntime and
ctranslate2 both publish cp314 wheels. No AUR package is required for the
benchmark.
