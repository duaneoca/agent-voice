# agentvoice

A drop in voice extension for Omarchy. Gives whatever AI agent is
installed on the box a natural voice interface: wake word or push to
talk in, spoken replies out. Claude Code is the first target, not the
only one.

Omarchy already ships dictation. Voxtype gives it push to talk on F9,
running whisper.cpp locally, with a Dictation indicator in the bar.
agentvoice is the other half: a wake word instead of a keypress, an
agent instead of a text field, and an answer spoken back.

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

## Principles

1. **Baseline, not bolted on.** Installs like any Omarchy extension:
   Quickshell plugin, Hyprland binds, systemd user service, config in
   ~/.config/agentvoice. Feels native to the desktop.
2. **Agent agnostic.** Follows `omarchy default agent`, so switching
   agents at the desktop switches the voice too. The voice layer never
   cares who is answering -- though the guarantees are not yet even:
   only Claude Code has been run end to end, and only Claude Code can
   raise a permission prompt.
3. **You own the mic.** Nothing listens unless you've enabled it, and
   the state is always visible. Linux has no OS level mic gate, so
   that guarantee is ours to build.
4. **Local first.** Wake word, STT, and TTS run on device. Only the
   agent call leaves the machine, if your agent does.
5. **Old hardware is the target, not the edge case.** Benchmarked on a
   2014 MacBook Pro, CPU only. If it needs a GPU it does not ship.

## Where Voxtype fits

An earlier draft of this file said agentvoice would drive Voxtype for
transcription and never open the microphone itself. That turned out not
to be possible, and it is worth writing down why, because the idea is an
obvious one to have twice.

Voxtype's running daemon exposes `record start|stop|toggle|cancel`, and
those operate on *its* microphone and type the result into the focused
window. There is no "transcribe this buffer" on the socket. The one
entry point that takes audio we already hold, `voxtype transcribe
<file>`, is a separate process that reloads the model on every call --
measured at 2.2s against ~320ms for an in-process model. And its capture
would only begin after the wake word had already been spoken, which is
the wrong moment.

So **agentvoice owns the whole input path** for wake word mode: capture,
wake word, endpointing, transcription. What it takes from Voxtype is its
shape, not its code -- a small state file the bar watches, a CLI of plain
verbs, models under XDG data.

**They coexist rather than compose.** F9 remains Voxtype's dictation and
is untouched; agentvoice listens for a phrase instead. Both want the same
microphone, which is a real constraint rather than a theoretical one:
running a wake word and holding F9 at the same time is asking one device
for two things.

## Input modes

1. **Wake word.** Say the trigger phrase, talk, get a spoken answer.
   Stays in conversation mode for a few seconds after each reply.
   Ours to build: Voxtype is push to talk only.
2. **Push to talk.** Voxtype's F9, unchanged and unintegrated. It
   dictates into the focused window, which is a different job.
3. **Mic toggle.** One key and one bar click to grab or release the
   microphone entirely.

Default binds (configurable):
  F9 (hold)            Voxtype dictation, untouched
  Super+Alt+Space      toggle wake word listening on/off
  Super+Ctrl+Space     cancel current response

Super+Space is Omarchy's application launcher and stays that way.

## Architecture

**Voice daemon** (Python, systemd user service)
Owns the microphone and everything done to what comes off it: wake word,
endpointing, transcription, conversation state, speech. Publishes what it
is doing to a state file the bar widget watches, and takes commands
through the `agentvoice` CLI (`start|stop|toggle|mic|status`), which
signals it rather than opening a socket -- the same shape Voxtype uses.

**Audio path**
100ms frames from PipeWire. Every frame is measured and dropped below the
gate, so nothing quieter than the room reaches the wake word. After the
wake word fires, the same frames feed both an endpointer and a buffer;
when a pause ends the turn, the buffer goes to Whisper in one piece.
While the machine is speaking the microphone is deaf, and the queue is
drained afterwards, because one microphone beside one speaker cannot
win -- see *Known risks*.

**Backend adapters**
One contract: take text plus a session ID, stream text back.
1. Claude Code: headless mode, streaming JSON, session resume
2. Other agent CLIs with non interactive modes (Codex, Gemini, etc.)
3. Generic OpenAI compatible HTTP: OpenAI, Grok, Ollama
Autodetect on install; choose the active one in config.

**Permissions**
A spoken sentence should not be able to edit files unchallenged. For
Claude Code, a PreToolUse hook passed through `--settings` receives each
tool call, writes it to the runtime directory, summons the overlay and
waits. The overlay shows the tool and the command verbatim with Deny,
Allow once, and a countdown; silence denies. Read-only tools are never
asked about, because a prompt on every Read would train you to say yes.

Not `--permission-prompt-tool`: that flag is accepted by the CLI and
does load the MCP server, but is never consulted -- tested against
2.1.278 in every permission mode. Hooks fire in all of them.

No other agent can raise a prompt from here. Adapters say so with
`guards_permissions`, and the ones that cannot are held in the safest
mode their CLI offers instead of its unattended one -- Gemini in
`--approval-mode plan`, the rest simply denied their bypass flag. That
fails closed by stalling rather than acting, which is the right
direction and a worse experience than asking.

**Desktop integration**
Quickshell plugin `duaneoca.agentvoice`, kinds `bar-widget` and
`overlay`. State icon (off, idle, listening, thinking, speaking),
click to toggle, and the hot mic indicator. The bar talks to the
daemon over IPC the way the Dictation indicator talks to Voxtype.
Omarchy 4 has no Waybar.

## Stack

Measured on the target hardware. See `bench/FINDINGS.md`.

**STT** faster-whisper `tiny.en`, in process, with a custom vocabulary
prompt. 4.9% WER against 6.5% for `base.en` with the same prompt, at
roughly half the latency, ~320ms after you stop talking. `small.en` is
disqualified: nearly two seconds of dead air on every turn.

**Custom vocabulary** is a first class component, not a tuning knob. A
fourteen word `initial_prompt` cut domain errors by 41% for 41ms.
That beat jumping two model sizes, which cost 1.8 seconds. Users edit
the term list; project names and tool names go in it.

**TTS** Piper `lessac-medium`. 22ms to first audio, RTF 0.045, which
is 22x realtime. espeak-ng is the fallback for machines that cannot
manage even that; it is robotic and costs 6ms.

**Wake word** two engines, chosen in settings, because they fail
differently.

*Vosk* (default) takes any phrase you like and costs 26MB. Its weakness
is not accuracy but the shape of the question it answers: a grammar has
to choose between the phrase and anything-else, so a phonetic neighbour
maps cleanly onto the phrase at full confidence. Measured here, `"hey
cloud"` scores **1.00** against a `"hey claude"` grammar, and `"apple
pie"` scores 0.89 against `"hey pi"`. A confidence threshold cannot fix
this; there is nothing to threshold.

*openWakeWord* has four pretrained phrases and real separation --
`"hey jarvis"` 0.996, a phonetic attack 0.898, unrelated speech 0.000 --
so 0.90 rejects the near miss and still fires. It costs ~154MB, almost
none of it the detector: scipy and scikit-learn are hard imports of its
package `__init__`. Pin `openwakeword==0.4.0`; 0.5 and later depend on
`tflite-runtime`, which has no wheel past cp311.

**Personal verifier** the only layer that knows *who* is speaking. Both
engines above are speaker independent by design, which is why a podcast
wakes them. `agentvoice-train-verifier` fits a logistic regression over
openWakeWord's embeddings from a couple of dozen clips of your voice,
against other speech and against other voices saying your phrase. It runs
only after the wake word has already fired and replaces that score.
Measured: no loss on the owner's voice, and it rejects every held-out
other-speaker clip the base model accepts. `agentvoice monitor --ab`
scores both on the same audio if you want to check your own.

**Echo cancel** not implemented. The plan is PipeWire
`module-echo-cancel`, capturing from the cancelled source node rather
than raw ALSA. Until then the microphone goes deaf while the machine
speaks, and for a configurable tail afterwards, which works but makes
barge in impossible. On the dev machine the module is not even loaded --
the only filter present is a speaker EQ.

## Open questions

1. **Does a verifier reject a real stranger?** Trained on the owner's
   voice it costs almost nothing and rejects everyone else: eleven live
   utterances fired 11/11 either way (mean 0.963 with, 0.987 without),
   while twelve held-out other-speaker clips were accepted 8/12 by the
   base model and **0/12** with the verifier. But those speakers are
   synthetic. A real person at the microphone is still untested.
2. **Streaming transcription.** faster-whisper cannot start before the
   utterance ends, which is why Vosk's partials exist at all: they give
   the screen something to show during the ~320ms wait. Voxtype's
   `whisper.eager_processing` transcribes overlapping chunks while you
   are still speaking; whether an equivalent is worth building here is
   open.
3. **Moonshine and Parakeet.** Both are built for low-latency short
   utterances and both appear in Voxtype's ONNX build. Either could beat
   `tiny.en` on the metric that matters -- time after you stop talking.
4. **Microphone contention.** agentvoice holds the microphone
   continuously in wake word mode. What that does to Voxtype's F9, and
   to any other recorder, is untested.

## What it costs to run

Roughly **600MB resident** and 5% of one core, idling. The models are what
weigh: whisper `tiny.en` 323MB, openWakeWord with its scipy and sklearn
179MB, a Piper voice 48MB, the Vosk model 114MB when the engine or the
live-transcript setting needs it. Only the models actually in use are
loaded, and switching engines releases the other one.

On disk it is about 750MB installed, or 900MB with openWakeWord as well.

That is comfortable on the 2014 laptop this was built on, which has 16GB.
It is not nothing on a 4GB machine, and principle 5 says that matters.

## Known risks

1. Laptop speakers into laptop mic: false wakes, broken barge in.
   Worst case on the dev machine, which has one mic and no array.
2. Thermal throttling. The dev machine logged 13,471 throttle events
   during benchmarking, and sustained inference degraded latency 2-3x.
   A long conversation gets slower. Budget for it.
3. Microphone gain. Shipped at +12dB on the dev machine, clipping 1.46%
   of samples at a normal speaking distance. Calibrate at install.
4. **Phonetic neighbours wake the Vosk engine.** Not a tuning problem: a
   grammar scores `"hey cloud"` exactly as high as `"hey claude"`. Use
   openWakeWord, and a verifier, if anything in the room talks.
5. **The permission guard is uneven.** Claude Code prompts on screen
   and has been tested end to end. Every other adapter is held in its
   CLI's safest mode instead, which is reasoning from their documented
   flags rather than something that has been run -- none of those CLIs
   is installed and signed in here.
6. Agent CLIs differ in headless and permission support. Only the Claude
   adapter has been run against a live agent; the Codex and Gemini
   adapters are written against observed flags and an unauthenticated
   CLI, and the nine others share one plain-stdout adapter.
7. **Cold stubs look installed.** Omarchy puts a mise stub on PATH for
   every agent it knows, so `command -v` finds all thirteen on a machine
   with none of them. Invoking one triggers a minute-long install.
   Adapters check for a real install, not just a name on PATH.

## Prior art (and the gap)

Voxtype: Omarchy's own dictation, local whisper.cpp, push to talk.
  Input only, types into the focused window. We coexist with it;
  driving it turned out not to be possible. See above.
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

Done:

1. Wake word to transcript, two engines, tunable from the bar
2. Claude Code adapter: headless, streaming, session resume
3. Replies written for the ear, spoken sentence by sentence
4. Quickshell plugin, settings overlay, install script, user service
5. Personal verifier training tool, and a permission prompt for
   Claude Code

Next, roughly in order of how much they matter:

6. Run the other adapters against live agents. Codex, Gemini and the
   nine sharing the CLI adapter have never answered a turn, and their
   permission posture is inferred from flags rather than tested.
7. Echo cancel, and barge in on top of it. Today the microphone simply
   goes deaf while the machine talks, so it cannot be interrupted.
8. Tests. `bin/agentvoice-check` catches syntax and settings that are
   declared but never drawn; nothing exercises behaviour.

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
