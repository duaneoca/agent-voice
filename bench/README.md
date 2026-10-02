# bench

Picks the default STT/TTS stack from measurements on the machine in front of
you, rather than from published numbers taken on other hardware. The figures
in `FINDINGS.md` were measured on a 2014 MacBook Pro (i7-4980HQ); yours will
differ, which is the reason this exists.

## Run it

```bash
./bench/setup.sh              # packages + models (polkit/sudo prompt once)
python bench/record.py        # ~2 min: read 18 prompts into your own mic
./bench/.venv/bin/python bench/stt_bench.py
./bench/.venv/bin/python bench/tts_bench.py
```

Results land in `results/*.json`.

## What it measures, and why

**`perceived_ms` is the headline, not RTF.** Vosk streams, so it consumes
audio while you are still speaking and only the tail is left when you stop.
Whisper is batch — it cannot begin until the utterance ends, so its entire
run time is dead air. On the hardware in FINDINGS.md, Whisper-tiny has the *better*
real-time factor and roughly ten times the perceived latency. Ranking on RTF alone would
pick the wrong engine.

**WER is scored without punctuation.** Vosk's small models emit none, and
penalising that would measure formatting rather than recognition.

**The corpus is your voice.** Published WER says nothing about this
microphone, this room, or the fact that you say "kubectl" and "Hyprland"
fifty times a day. `prompts.txt` is tagged `short` / `plain` / `domain` /
`long`, because those four behave very differently and the averages hide it.

`--corpus <dir>` points the STT run at an alternative corpus — useful for
comparing a synthetic set against real speech.

## Layout

```
setup.sh          packages, venv, model downloads
record.py         interactive corpus recorder (16k mono)
stt_bench.py      Vosk + faster-whisper: WER, perceived latency, RTF, RSS
tts_bench.py      espeak-ng + Piper: time-to-first-audio, RTF
corpus/           prompts.txt, ground_truth.tsv, wav/
results/          stt-*.json, tts.json
```

The venv is built by `uv` on its own CPython 3.13, from the same
`requirements.txt` and `requirements-openwakeword.txt` the product installs --
so a measurement describes the stack that ships, and it does not break when
Arch bumps its system Python. It used to be the system interpreter with
`--system-site-packages` and a pacman `python-vosk`; it is not any more.

Those requirements give version *ranges*, not pins, so two runs weeks apart
can resolve different wheels. Record the resolved versions alongside any
number worth keeping.
