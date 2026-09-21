# agentvoice

A drop in voice extension for Omarchy. Gives whatever AI agent is
installed on the box a natural voice interface: wake word or push to
talk in, spoken replies out. Claude Code is the first target, not the
only one.

## Principles

1. **Baseline, not bolted on.** Installs like any Omarchy extension:
   Waybar module, Hyprland binds, systemd user service, config in
   ~/.config/agentvoice. Feels native to the desktop.
2. **Agent agnostic.** Detects which agent CLIs are installed and uses
   the one you pick. The voice layer never cares who's answering.
3. **You own the mic.** Nothing listens unless you've enabled it, and
   the state is always visible.
4. **Local first.** Wake word, VAD, STT, and TTS run on device. Only
   the agent call leaves the machine, if your agent does.

## Input modes

1. **Wake word.** Say the trigger phrase, talk, get a spoken answer.
   Stays in conversation mode for a few seconds after each reply.
2. **Push to talk.** Hold a key to record, release to send. Uses
   Hyprland's press and release binds. Works even when the wake
   word is off.
3. **Mic toggle.** One key and one Waybar click to grab or release
   the microphone entirely.

Default binds (configurable):
  Super+Space (hold)   push to talk
  Super+Shift+Space    toggle mic on/off
  Super+Ctrl+Space     cancel current response

## Architecture

**Voice daemon** (Python, systemd user service)
Owns all audio: wake word, VAD endpointing, STT, TTS, echo cancel,
conversation state. Exposes a small local socket so binds and Waybar
can send it commands (ptt_start, ptt_stop, toggle, cancel, status).

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
Prototype with zenity or mako actions; real version is a GTK4 layer
shell window styled from the active Omarchy theme.

**Desktop integration**
Waybar module: state icon (off, idle, listening, thinking, speaking),
click to toggle. Doubles as the hot mic indicator.
Hyprland binds as above. Install script drops both in place.

## Initial stack

openWakeWord, Silero VAD, faster-whisper (small or base.en),
Piper or Kokoro TTS, PipeWire echo cancel module.
STT custom vocabulary (Omarchy, Hyprland, kubectl, project names)
via Whisper's initial prompt.

## Known risks

1. Laptop speakers into laptop mic: false wakes, broken barge in
2. Endpointing and time to first token dominate latency
3. Per turn CLI startup cost; prefer a long lived session
4. Agent CLIs differ in headless and permission support

## Prior art (and the gap)

voice-to-claude: local wake word dictation for Claude Code on Linux.
  Input only, types into focused window, no spoken replies.
tryvoice: adapter registry for Claude Code, OpenClaw, custom.
  Browser and cloud leaning, not a native desktop daemon.
talk-to-claude, VoiceMode: MCP servers driven from inside a Claude
  session. The agent runs the loop, not the desktop.
Claude Code native voice: hold Space only, no wake word yet.

None is a desktop level, agent agnostic voice layer. That's this.

## Build order

1. Mic capture with echo cancel working
2. Push to talk: Hyprland bind to STT to printed text, no AI
3. Wake word into the same path
4. Claude Code adapter, headless with session resume
5. TTS with sentence by sentence streaming, conversation mode
6. Permission MCP tool and popup
7. Waybar module, install script, systemd service
8. Second adapter to prove the contract holds

## Layout (proposed)

agentvoice/
  daemon/        audio pipeline, conversation state, control socket
  adapters/      base.py, claude_code.py, openai_compat.py
  permissions/   mcp server, popup UI
  desktop/       waybar module, hyprland binds, systemd unit
  install.sh
  config.toml