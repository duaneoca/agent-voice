# Working on agentvoice

## Before every commit

```bash
./bin/agentvoice-check
```

Four static checks plus the test suite, about two seconds. It exists because
this project shipped a string of bugs that reached the user first: an unbound
shell variable, a gum box rendered 298 columns wide, a Python method whose
body was orphaned into dead code after a `return`, and three settings that
were declared in the manifest, wired through the daemon, and never drawn in
the UI. Every one of those is the kind a second of automation catches.

## What earns a test

Add one when you touch:

- **`speech_text.py`** — sentence boundaries and markdown flattening. Every
  rule there exists because something sounded wrong when spoken aloud.
- **`permission_hook.py`** — a bug here fails open. Anything that changes how
  a decision is reached needs a case proving silence, corruption and malformed
  input all still deny.
- **`runtime.Config`** — precedence and reload. A value the daemon read once
  and never again has caused two separate "it does nothing" bugs.
- **`adapters/`** — the streaming contract, the cold-stub check, and the
  permission posture of each backend under `ask_permission` on and off.

Do not try to test the QML, the audio loop, or anything needing a microphone.
Those are exercised by hand, and `agentvoice monitor` exists so a wake word
can be diagnosed with numbers rather than guesses.

## Settings are three things, not one

A new setting is not done until all three exist, and the check script enforces
the last two:

1. `manifest.json` → `barWidget.schema` and `barWidget.defaults`
2. `daemon/runtime.py` → `DEFAULTS`, and `TOML_ALIASES` if it belongs in the
   fallback config
3. `Settings.qml` → an actual control

Skipping step 3 gives a setting that works from `omarchy bar set` and is
invisible in the UI. That has happened.

## Anything a model loads is reconciled, not constructed once

`Pipeline.apply()` decides when to rebuild the wake engine, the transcription
model and the speaker. A new model-backed setting belongs in the spec it
compares, or changing it will silently do nothing until the service restarts.
Include a file mtime when the artifact can be rewritten underneath it — that
is why the verifier's mtime is in the wake spec.

Release what you replace. Dropping the reference is not enough: glibc keeps
the arena and RSS barely moves, so call `Pipeline._release()`, which also
runs `malloc_trim(0)`.

## Claims in the README and in commits

Measure before asserting. `bench/FINDINGS.md` records what was measured, on
what hardware, with the numbers. If a claim there turns out to be wrong,
correct it in place rather than leaving two versions of the truth — the
Voxtype-transcription architecture and the "31% of one core" figure were both
wrong, and both are now written down as corrections with the reasoning.

Say which backends have actually answered a turn. Claude Code and Gemini
have; nothing else. Watching Gemini for a single live turn falsified three
things its adapter believed -- where the CLI records its auth, that a
headless run needs no trusted folder, and that everything on stdout was
speech. Flags read off `--help` are a hypothesis, not a contract.
