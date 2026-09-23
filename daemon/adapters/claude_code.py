"""Claude Code, headless, streaming, with session resume."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import Iterator

from .base import Adapter, Chunk, SPOKEN_STYLE, Watchdog, installed

#: Tools that can change something or reach the network. Read-only tools are
#: not listed: asking about every Read would train you to say yes.
GUARDED_TOOLS = "Bash|Write|Edit|MultiEdit|NotebookEdit|WebFetch|KillShell"


class ClaudeCode(Adapter):
    name = "claude"
    guards_permissions = True
    # The only backend with somewhere to put the question: a PreToolUse hook
    # receives the pending call and blocks on the verdict, so "edits" can be
    # decided per call against the project path.
    levels = ("ask", "edits", "trusted")

    # `auto` is the mode Omarchy's own `omarchy agent` uses for unattended
    # launches. Until the permission overlay exists there is nothing to answer
    # a prompt with, so a mode that can block is the wrong default.
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
        import json
        import sys
        from pathlib import Path

        hook = Path(__file__).resolve().parent.parent / "permission_hook.py"
        return json.dumps({"hooks": {"PreToolUse": [{
            "matcher": GUARDED_TOOLS,
            # The agent's own name travels with the hook: permission levels
            # are per agent now, and the hook is a separate process that
            # cannot otherwise know which one spawned it.
            "hooks": [{"type": "command",
                       "command": f"{sys.executable} {hook} {cls.name}"}],
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
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start claude: {e}")
            return

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
                yield Chunk(error=f"{"claude"} stopped responding after "
                                  f"{dog.idle_s:.0f}s")
                return
            if code != 0:
                err = (proc.stderr.read() or "").strip()
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
            proc.terminate()
