"""Claude Code, headless, streaming, with session resume."""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterator

from .base import (
    Adapter, Chunk, Drain, SPOKEN_STYLE, Watchdog, installed, stop_tree,
)

#: Every tool. The hook's `matcher` is also the limit of what the permission
#: system can see, so naming the dangerous ones here put the rest outside it
#: entirely. Which tools are free is decided in permission_hook.py, where it
#: can be read, tested and said out loud -- see NO_EFFECT and READ_ONLY.
GUARDED_TOOLS = ".*"


class ClaudeCode(Adapter):
    name = "claude"
    guards_permissions = True
    remembers = True          # --resume <session-id>
    # The only backend with somewhere to put the question: a PreToolUse hook
    # receives the pending call and blocks on the verdict, so "edits" can be
    # decided per call against the project path.
    levels = ("ask", "edits", "trusted")

    # `auto` is the mode Omarchy's own `omarchy agent` uses for unattended
    # launches, and the one this runs in: the question is answered by the
    # PreToolUse hook and the overlay, not by Claude's own prompt, which has
    # no terminal to appear in. A mode that can block would simply hang.
    def __init__(self, model: str | None = None,
                 permission_mode: str = "auto",
                 cwd: str | None = None,
                 spoken: bool = True,
                 ask_permission: bool = True):
        self.model = model
        self.spoken = spoken
        self.ask_permission = ask_permission
        self.permission_mode = permission_mode
        self.cwd = cwd or os.getcwd()
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @classmethod
    def _hook_settings(cls) -> str:
        """A PreToolUse hook, passed inline as JSON.

        `--settings` takes a JSON string and loads it *in addition to* the
        user's own settings, so this adds the hook without disturbing anything
        they have configured.

        A hook rather than `--permission-prompt-tool`: that flag is accepted
        by the CLI and loads the MCP server, but is never consulted -- tested
        against 2.1.278 across every permission mode. Hooks fire in all of
        them, including `auto`.
        """
        hook = Path(__file__).resolve().parent.parent / "permission_hook.py"
        # Quoted, because this string is run by a shell. A space anywhere in
        # the interpreter path or the plugin path -- and the plugin lives
        # under $HOME -- would split the command, give exit 127, and a hook
        # that cannot start is read by Claude Code as a non-blocking error:
        # the tool proceeds. Failing to quote here disables the guard.
        command = " ".join(shlex.quote(part)
                           for part in (sys.executable, str(hook), cls.name))
        return json.dumps({"hooks": {"PreToolUse": [{
            "matcher": GUARDED_TOOLS,
            # ".*" -- every tool, with permission_hook.py deciding which ones
            # are free. A matcher that named the dangerous tools left every
            # other one outside the permission system altogether: `mcp__*`
            # from the user's own servers (send mail, post to Slack, write a
            # calendar), WebSearch, whose query *is* the exfiltration, Read
            # of any path at all, and Task. Meanwhile the settings screen
            # said "asks before every change".
            #
            # The cost is a process per tool call. The free tools return
            # before importing anything but json and os, which is where that
            # cost is paid back.
            # The agent's own name travels with the hook: permission levels
            # are per agent now, and the hook is a separate process that
            # cannot otherwise know which one spawned it.
            "hooks": [{"type": "command", "command": command}],
        }]}})

    def available(self) -> bool:
        return installed("claude")

    def _argv(self, text: str, session_id: str | None) -> list[str]:
        argv = [
            "claude", "-p", text,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",                      # stream-json requires it
            "--permission-mode", self.permission_mode,
        ]
        if self.spoken:
            argv += ["--append-system-prompt", SPOKEN_STYLE]
        if self.ask_permission:
            argv += ["--settings", self._hook_settings()]
        if session_id:
            argv += ["--resume", session_id]
        if self.model:
            argv += ["--model", self.model]
        return argv

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        argv = self._argv(text, session_id)
        # The environment is inherited whole, including AGENTVOICE_ENDPOINT_KEY
        # and OPENAI_API_KEY, which any Bash command the agent runs can read.
        # Deliberately: Codex authenticates with OPENAI_API_KEY, so scrubbing
        # it here would break a backend to protect a key from an agent the
        # user is already trusting to run commands on their behalf. The key
        # that matters is not in the environment on this machine anyway --
        # endpoint_key() prefers the login keyring.
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1,
                                    start_new_session=True)
        except OSError as e:
            yield Chunk(error=f"could not start claude: {e}")
            return

        # Drained in the background from here on: stderr is a 64KB pipe
        # and it is not read until stdout ends, so a chatty CLI fills it,
        # blocks on its next write, and stops producing stdout -- which the
        # watchdog then reports as "stopped responding".
        errors = Drain(proc.stderr)

        with self._lock:
            self._proc = proc
        dog = Watchdog(proc, self.idle_timeout_s)

        try:
            for line in proc.stdout:
                dog.poke()
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = msg.get("type")

                if kind == "system" and msg.get("subtype") == "init":
                    yield Chunk(session_id=msg.get("session_id"))

                elif kind == "stream_event":
                    event = msg.get("event") or {}
                    etype = event.get("type")
                    if etype == "message_start":
                        if msg.get("ttft_ms") is not None:
                            yield Chunk(ttft_ms=msg["ttft_ms"])
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            yield Chunk(text=delta.get("text", ""))
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            yield Chunk(tool=block.get("name"))

                elif kind == "result":
                    if msg.get("subtype") != "success" and msg.get("is_error"):
                        yield Chunk(error=str(msg.get("result") or "agent error"))
                    yield Chunk(done=True,
                                session_id=msg.get("session_id"),
                                cost_usd=msg.get("total_cost_usd"),
                                duration_ms=msg.get("duration_ms"))

            code = proc.wait()
            if dog.fired:
                # Killed for going quiet. Indistinguishable from a cancel by
                # exit code alone, and silence is the one thing a voice
                # interface must never answer with.
                yield Chunk(error=f"claude stopped responding after "
                                  f"{dog.idle_s:.0f}s")
                return
            if code != 0:
                err = errors.text()
                # A cancel closes the pipe under us; that is not a failure.
                if code not in (-15, 143, -9, 137):
                    yield Chunk(error=err or f"claude exited {code}")
        finally:
            dog.stop()
            with self._lock:
                self._proc = None
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            stop_tree(proc, signal.SIGTERM)
