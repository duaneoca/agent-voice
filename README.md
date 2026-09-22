# agentvoice

A drop in voice extension for Omarchy. Gives whatever AI agent is
installed on the box a natural voice interface: wake word or push to
talk in, spoken replies out. Claude Code is the first target, not the
only one.

Omarchy already ships the input half. Voxtype gives it push to talk
dictation on F9, running whisper.cpp locally, with a Dictation
indicator in the bar. agentvoice does not rebuild any of that. It
builds the half that is missing: the agent hears you, and answers out
loud.

## Principles

1. **Baseline, not bolted on.** Installs like any Omarchy extension:
   Quickshell plugin, Hyprland binds, systemd user service, config in
   ~/.config/agentvoice. Feels native to the desktop.
2. **Agent agnostic.** Detects which agent CLIs are installed and uses
   the one you pick. The voice layer never cares who's answering.
3. **You own the mic.** Nothing listens unless you've enabled it, and
   the state is always visible. Linux has no OS level mic gate, so
   that guarantee is ours to build.
4. **Local first.** Wake word, STT, and TTS run on device. Only the
   agent call leaves the machine, if your agent does.
5. **Old hardware is the target, not the edge case.** Benchmarked on a
   2014 MacBook Pro, CPU only. If it needs a GPU it does not ship.

## What we own, and what we don't

**Voxtype owns input.** Microphone capture, whisper.cpp inference,
model management, push to talk binds, the bar indicator. It exposes
everything we need: `record start|stop|toggle|cancel`, a JSON status
command, a state file, a unix socket, `whisper.initial_prompt` for
custom vocabulary, and `whisper.mode = remote` for machines too slow
to run a model at all.

**agentvoice owns the conversation.** Wake word, conversation state,
the agent adapters, spoken replies, barge in, and tool permission
prompts. Voxtype types a transcript into whatever window has focus; it
has no idea an agent exists. That gap is the project.

## Input modes

1. **Wake word.** Say the trigger phrase, talk, get a spoken answer.
   Stays in conversation mode for a few seconds after each reply.
   Ours to build: Voxtype is push to talk only.
2. **Push to talk.** Voxtype's F9, unchanged. We read the transcript
   rather than reimplementing the capture path.
3. **Mic toggle.** One key and one bar click to grab or release the
   microphone entirely.

Default binds (configurable):
  F9 (hold)            push to talk, Voxtype's existing bind
  Super+Alt+Space      toggle wake word listening on/off
  Super+Ctrl+Space     cancel current response

Super+Space is Omarchy's application launcher and stays that way.

## Architecture

**Voice daemon** (Python, systemd user service)
Owns wake word detection, conversation state, TTS playback, and echo
cancel. Drives Voxtype for transcription rather than opening the mic
itself. Exposes a small local socket so binds and the bar can send it
commands (wake_on, wake_off, cancel, status).

**Transcript handoff**
Voxtype hands text back through `output.post_process.command`, which
receives the transcription on stdin, or `output.mode = file`. Either
way agentvoice gets text, not keystrokes.

**Backend adapters**
One contract: take text plus a session ID, stream text back.
1. Claude Code: headless mode, streaming JSON, session resume
2. Other agent CLIs with non interactive modes (Codex, Gemini, etc.)
3. Generic OpenAI compatible HTTP: OpenAI, Grok, Ollama
Autodetect on install; choose the active one in config.

**Permissions**
For Claude Code, `--permission-prompt-tool` points at a local MCP
server run by the daemon. Tool requests pop a window; click or say
"approve" / "deny", whichever comes first. Timeout defaults to deny.
Other adapters map their own approval flow onto the same popup.
An Omarchy `overlay` plugin, themed from the active theme, rather than
a GTK4 window we style ourselves.

**Desktop integration**
Quickshell plugin `duaneoca.agentvoice`, kinds `bar-widget` and
`overlay`. State icon (off, idle, listening, thinking, speaking),
click to toggle, and the hot mic indicator. The bar talks to the
daemon over IPC the way the Dictation indicator talks to Voxtype.
Omarchy 4 has no Waybar.

## Stack

Measured on the target hardware. See `bench/FINDINGS.md`.

**STT** whisper `tiny.en` through Voxtype, with a custom vocabulary
prompt. 6.5% WER against 6.9% for `base.en` with the same prompt, at
roughly half the latency. `small.en` is disqualified: nearly two
seconds of dead air after every utterance.

**Custom vocabulary** is a first class component, not a tuning knob. A
fourteen word `initial_prompt` cut domain errors by 41% for 41ms.
That beat jumping two model sizes, which cost 1.8 seconds. Users edit
the term list; project names and tool names go in it.

**TTS** Piper `lessac-medium`. 22ms to first audio, RTF 0.045, which
is 22x realtime. espeak-ng is the fallback for machines that cannot
manage even that; it is robotic and costs 6ms.

**Wake word** undecided, pending the `eager_processing` test below.
Vosk is the candidate: pacman installable from `extra`, 30ms, and its
grammar restriction pins the trigger phrase far harder than a generic
model. It does not need to be accurate, only to notice a phrase.

**Echo cancel** PipeWire `module-echo-cancel`. Capture from the
cancelled source node, not raw ALSA, or the speakers feed straight
back into the mic.

## Open questions

1. `whisper.eager_processing` transcribes overlapping chunks while you
   are still speaking. If it delivers, Whisper gets Vosk's streaming
   latency and the wake word component may not need Vosk at all.
   Untested. Highest value experiment remaining.
2. Voxtype's ONNX build carries Moonshine and Parakeet, both built for
   low latency. The AVX2 build we run has whisper only. Worth a look.
3. Whether the transcript handoff should be `post_process.command` or
   the unix socket. The socket carries live audio levels and may carry
   more.

## Known risks

1. Laptop speakers into laptop mic: false wakes, broken barge in.
   Worst case on the dev machine, which has one mic and no array.
2. Thermal throttling. The dev machine logged 13,471 throttle events
   during benchmarking, and sustained inference degraded latency 2-3x.
   A long conversation gets slower. Budget for it.
3. Microphone gain. Shipped at +12dB on the dev machine, clipping 1.46%
   of samples at a normal speaking distance. Calibrate at install.
4. Agent CLIs differ in headless and permission support.
5. Building on Voxtype means tracking its config schema.

## Prior art (and the gap)

Voxtype: Omarchy's own dictation, local whisper.cpp, push to talk.
  Input only, types into the focused window. We build on it.
voice-to-claude: local wake word dictation for Claude Code on Linux.
  Input only, no spoken replies.
tryvoice: adapter registry for Claude Code, OpenClaw, custom.
  Browser and cloud leaning, not a native desktop daemon.
talk-to-claude, VoiceMode: MCP servers driven from inside a Claude
  session. The agent runs the loop, not the desktop.
Claude Code native voice: hold to talk, and the transcription is a
  hosted service rather than a local model.

None is a desktop level, agent agnostic, speaking voice layer.
That's this.

## Build order

1. Wake word prototype: trigger phrase to printed transcript
2. Transcript handoff from Voxtype into the daemon
3. Claude Code adapter, headless with session resume
4. TTS with sentence by sentence streaming, conversation mode
5. Echo cancel and barge in
6. Permission MCP tool and overlay
7. Quickshell plugin, install script, systemd service
8. Second adapter to prove the contract holds

## Install

The bar widget is an Omarchy shell plugin, so it arrives the usual way:

```bash
omarchy plugin add https://github.com/duaneoca/agent-voice.git --enable
```

That gets you the icon and the settings screen. The thing that listens is a
Python daemon with a few hundred megabytes of models behind it, so it is a
second, explicit step -- the way Omarchy installs Voxtype:

```bash
~/.config/omarchy/plugins/duaneoca.agentvoice/install.sh
```

It needs one system package, `uv` from the official `extra` repo, and then
manages its own CPython. Nothing is installed against the system interpreter:
Arch is a rolling release, and a venv built on `/usr/bin/python` stops
importing the next time Arch bumps it, which would leave voice silently dead
after an `omarchy update`.

Roughly 750MB with the default engine, or 900MB with openWakeWord as well;
the installer says what the second engine costs before installing it.

## Layout

```
agent-voice/              <- also the plugin directory once installed
  manifest.json           kinds: bar-widget + overlay
  Panel.qml               bar icon, state, last exchange, switch
  Settings.qml            the knobs, as a themed overlay
  KnobRow.qml             shared slider row
  install.sh
  requirements.txt                   core: vosk, whisper, piper
  requirements-openwakeword.txt      optional second wake engine
  bin/
    agentvoice                       start|stop|toggle|mic|status
    agentvoice-train-verifier        guided personal-verifier training
  daemon/
    paths.py              one place that resolves every path
    wake_listen.py        the loop: wake -> capture -> agent -> speech
    runtime.py            config, state file, speech
    speech_text.py        markdown -> speakable prose, sentence chunking
    verifier.py           train and load a personal verifier
    adapters/             base contract + claude_code, codex, gemini, ...
    agentvoice.toml       fallback config for non-Omarchy machines
    vocab.txt             default custom vocabulary
  desktop/
    agentvoice.service.in systemd user unit template
  bench/                  hardware benchmark and findings
```

Everything the installer downloads -- interpreter, wheels, models, trained
verifiers -- lives under `~/.local/share/agentvoice/`, never in the repo.
