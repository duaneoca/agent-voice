# agentvoice

[![check](https://github.com/duaneoca/agent-voice/actions/workflows/check.yml/badge.svg)](https://github.com/duaneoca/agent-voice/actions/workflows/check.yml)

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
   Claude Code, Codex, Gemini and Antigravity have answered live turns,
   the nine sharing the CLI adapter have not, and only Claude Code can
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
2. **Push to talk.** Hold F8, speak, release. Skips the wake word
   entirely, which makes it the reliable path when the room is noisy or
   the wake word is being stubborn. Distinct from Voxtype's F9, which
   dictates into the focused window -- a different job.
3. **Mic toggle.** One key and one bar click to grab or release the
   microphone entirely.
4. **Interrupt.** Press F8 while it is replying, or Super+Ctrl+Space, or
   the stop button in the panel. Deliberately not voice-triggered -- see
   below.

Suggested binds. `install.sh` prints these rather than writing them,
because `~/.config/hypr/bindings.lua` is yours:

  F8 (hold)            talk to the agent; a press mid-reply stops it
  F9 (hold)            Voxtype dictation, untouched
  Super+Alt+Space      release or re-engage the mic
  Super+Ctrl+Space     stop talking, keep listening

Super+Space is Omarchy's application launcher and stays that way.

### Why you cannot interrupt by voice

The obvious design is to say "stop" over the reply. Measured on this
hardware -- one laptop microphone about a foot from the speaker it is
listening to, at a normal listening volume -- that does not work:

  wake word detected in a quiet room       19/20
  wake word detected over our own reply     5/20

The reason is simple once measured: our own playback arrives at the
microphone at the same level as your voice. Signal to noise is +1.2 dB,
and the detector needs about +24 dB to be reliable. That is a ~23 dB
deficit, and a dedicated "interrupt" phrase would be swamped identically
-- the failure is acoustic masking, not vocabulary.

The detector never false-fires on our own voice: peak score 0.001 at every
volume up to 100%, against a 0.75 threshold. Listening while talking is
safe; hearing over it is not.

PipeWire's echo canceller does fix it, and completely:

  no cancellation              SNR  +1.2 dB    5/20
  module-echo-cancel, converged SNR +13.4 dB   19/20

Two conditions keep it out of the default install. Its stock settings
enable webrtc's noise suppressor and AGC, which gate the wake word into
unintelligibility (0/20) while appearing, if you only look at median
levels, to be cancelling beautifully. And the adaptive filter needs
roughly 30 seconds of continuous output to converge -- far longer than a
spoken reply -- so it is unconverged exactly when a short answer needs it.

Half duplex with an explicit interrupt is honest about the hardware;
acoustic barge-in is an open question, not a feature.

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
One contract: take text plus a session ID, stream text back. Which agent
answers follows `omarchy default agent`, unless an endpoint URL is set,
which wins because the desktop has no entry for "the model on my other
machine". That rule is reasonable and surprising in equal measure --
choosing an agent in Omarchy's settings and having nothing change looks
exactly like a broken setting -- so the panel names what is being
overridden (`endpoint · … · instead of claude`) and clearing both
endpoint boxes hands control back.

Eight have answered a live turn. The columns that decide how it feels to
talk to are not the ones that decide how capable it is:

| backend | reached by | remembers | tools | a two-sentence answer |
|---|---|---|---|---|
| Claude Code | CLI, headless | yes | yes | — |
| Codex | CLI, `exec --json` | yes | yes | 2.8s |
| Antigravity (`agy`) | CLI, `-p --output-format json` | yes | yes | 12.2s |
| Groq | endpoint | yes | no | 0.19s |
| OpenAI | endpoint | yes | no | 1.9s |
| xAI Grok | endpoint | yes | no | 3.2-4.6s |
| Ollama, LM Studio, vLLM | endpoint | yes | no | 7.6s on a LAN 4B |
| **Gemini** | CLI | **no** | yes | — |
| the ten on the shared CLI adapter | CLI | **no** | yes | untested |

**Two of these act here, and one acts somewhere else.** Claude Code,
Codex, Antigravity and Gemini are started by this daemon, in a directory
it chooses, under flags it passes -- so the project and permission
settings mean something, and what they protect is this machine. An
endpoint is a URL. agentvoice gives it no flags, no hook and no sandbox,
and cannot see what is behind it.

Usually that is a chat completion which can do nothing at all. Sometimes
it is an agent: Hermes speaks the same wire format, and its own
documentation calls its bearer token equivalent to a root password --
terminal and filesystem on the Hermes host. Asked for the hostname it was
running on, it answered with it.

The difference is not how dangerous it is, it is *where*. Nothing this
machine holds is at risk from Hermes; the Mac mini it runs on is. Which
also means the project directory and permission level do not apply --
they govern an agent started here, and reach nothing over there, so the
panel drops them and the settings screen says why. Restrain a remote
agent where it runs.

Tick **This endpoint is an agent** when the endpoint can act. agentvoice
cannot tell from the URL, so until you say, it claims nothing about the
far end -- only that it offers no tools itself.

**Remembering is three different mechanisms, and one absence.** Claude
Code, Codex and Antigravity each keep the conversation themselves and
hand back a handle -- `--resume`, a `thread_id`, `--conversation`. The
endpoint keeps no server-side session at all, so the transcript is ours:
it is replayed up the wire every turn, capped at eight turns, and every
replayed turn is re-read and re-billed. Gemini's headless mode is
one-shot and returns no handle of any kind.

That last row is the one to know about, because it fails quietly.
Conversation mode still opens its window after a Gemini reply, still
listens, still answers -- and every turn starts from nothing. Ask "what
did you mean by that?" and you get an answer to a question it has never
seen. The same is true of the ten agents sharing the plain-stdout
adapter, none of which has been run.

Two smaller things that follow from the mechanism. An interrupted
endpoint turn never enters the history, because the history is written
when the stream ends -- so the machine forgets its own half of an
exchange you cut short. And the follow-up window is shortened by
`echoTailMs`, which is counted from the same moment, so a 4s window with
a 350ms echo tail is really 3.65s.

Switching agent or project drops the session id, deliberately: a handle
from one project resumes the wrong conversation in another.

**Project and permissions**
The agent works in one directory, and what it may do is a property of
that directory and of the agent -- shown together on the panel so you can
see before you speak what you are about to affect and how much it can do
without asking. Empty means your home directory, which is what a bare
`claude` does.

  ask       every change is prompted
  edits     files inside the project may be edited without asking;
            commands, web fetches and anything outside it still prompt
  trusted   nothing is asked

**The level is per agent, and absence is never permission.** Trust is a
judgement about one program's capabilities, and those differ enormously:
Claude Code can be stopped mid-call by a hook we answer, Codex cannot be
stopped at all. A single global level meant that trusting Claude Code
silently handed the same trust to whatever `omarchy default agent` was
switched to next. Levels are stored per agent, an agent nobody has
decided about reads as `ask`, and switching agents at the desktop is
picked up live rather than at the next restart.

Each backend offers only the levels it can honour, and the panel names
what is actually in force -- `claude · edits here · commands ask`, or
`codex · read-only (codex cannot ask)`. `edits` needs somewhere to put
the question or a flag that means it, so today only Claude Code has it.
Offering three levels where one is a lie is how a screen ends up
promising a guarantee that is not running.

`edits` is the level that stops you approving reflexively: editing files
is frequent and legible, running shell commands is rare and dangerous, so
they are separated. Bash is never path-scoped at any level -- `cd /; rm
-rf .` carries no file path, and no prefix match makes one safe to infer.
Path rules compare fully resolved paths, so `..` and symlinks cannot walk
out of a trusted project.

Changing the project drops the session id, because `--resume` is scoped
per project in Claude Code and would otherwise resume the wrong
conversation.

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
5. **The permission guard is uneven.** Only Claude Code can put a
   question on screen, through a PreToolUse hook that fires whether the
   model likes it or not. Codex, Gemini and Antigravity are held in
   their CLI's safest mode instead, so they refuse work rather than ask
   for it. Antigravity has a hook with a richer vocabulary and it cannot
   grant: in headless mode `allow` is ignored, so a hook there can
   restrict and never permit.
6. **Not every backend remembers the last thing you said.** Gemini and
   the ten on the shared CLI adapter start from nothing on every turn,
   and conversation mode gives no sign of it -- see *Backend adapters*.
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
6. Push to talk on F8, and an interrupt. Barge-in by voice was measured
   and rejected rather than skipped: at a normal listening volume our
   own playback reaches the microphone as loudly as the speaker does
   and detection falls to 5/20, where reliable wake needs about +24 dB
   of headroom. Echo cancellation closes the gap but wants ~30s of
   continuous output to converge, which is longer than a reply. The
   numbers are in `bench/FINDINGS.md`.
7. A project directory the agent works in, and permission levels that
   are per agent, because trust is a judgement about one program's
   capabilities and cannot survive swapping the program.
8. Eight backends answering live turns: Claude Code, Codex, Gemini,
   Antigravity, and -- through one OpenAI-compatible adapter -- Ollama,
   OpenAI, Groq and xAI Grok. The endpoint path was 114 lines that had
   never run; it is now the fastest backend here and the only one whose
   wire format never had to be guessed.
9. Keys in the login keyring rather than a file, looked up by endpoint
   host and port so two services on one machine do not share one.
10. A remote agent: Hermes, over the same endpoint adapter, turning
    office lights on and off through Home Assistant by voice. Its
    tool-activity chunks passed through the parser, speech started
    before the tool finished and resumed after it, and the follow-up
    window carried the second command without a wake word.

Next, roughly in order of how much they matter:

9. Run the remaining adapters against live agents. The ten sharing the
   CLI adapter never have, and every one is an Omarchy mise stub here,
   so each needs a real install first. Their `remembers = False` is an
   inference from our own adapter yielding no session id, not a
   measurement of the CLI -- if one is ever installed, check whether it
   hands back a handle we are throwing away.

   The rate of wrong guesses so far: three in Gemini's adapter, none in
   Codex's, one security claim withdrawn from Antigravity's, and one
   Cloudflare block that made every Groq model unreachable. Flags read
   off `--help` are a hypothesis.
10. Antigravity is in Omarchy's default branch (`quattro`) but not in
    any release: `gemini` and `gemini-cli` alias to `agy` there, which
    is the name this already registers. Until that ships, selecting it
    means writing `~/.config/omarchy/defaults/agent` directly.
11. Widen the tests. `bin/agentvoice-check` runs 230, covering the
    speakable-text rules, the permission hook and its levels, config
    precedence, key resolution and scope, the adapter contract,
    process-tree teardown, and the recorded envelopes of the backends
    that have answered. The wake loop and the QML are still exercised
    only by hand.
12. A stranger at the microphone. The verifier's 0/12 against other
    speakers was synthetic, and a second voice in the room is the one
    failure mode that cannot be tested alone.
13. Conversation mode on a backend that forgets. The warning is
    verified; the behaviour behind it is not, because the only forgetful
    backend installed here is 503ing.

## Development

```bash
VIRTUAL_ENV=~/.local/share/agentvoice/venv uv pip install -r requirements-dev.txt
./bin/agentvoice-check          # static checks + tests, about two seconds
```

`CLAUDE.md` has the working rules: what earns a test, why a setting is three
things rather than one, and how model reloading is reconciled.

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
  tests/                  pytest suite, run by bin/agentvoice-check
  bench/                  hardware benchmark and findings
```

Everything the installer downloads -- interpreter, wheels, models, trained
verifiers -- lives under `~/.local/share/agentvoice/`, never in the repo.

## License

MIT, in `LICENSE` and declared in `manifest.json`. Both have to say the
same thing: Omarchy reads the manifest, GitHub reads the file, and a
manifest claiming a licence the repo does not carry grants nothing.

The models are not covered by it. Nothing here redistributes them --
`install.sh` downloads Whisper, Piper, Vosk and openWakeWord from their
own projects at install time, and each carries its own terms. A trained
verifier is yours and stays on your machine; it is a pickle, so it must
never be one you downloaded.
