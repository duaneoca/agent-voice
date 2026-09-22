"""Codex CLI, headless, via `codex exec --json`.

Partly verified. The envelope was confirmed against the installed CLI
(codex-cli 0.154.0): it emits newline-delimited JSON with `thread.started`
(carrying `thread_id`, which is the handle `codex exec resume` takes),
`turn.started`, `item.completed`, `error` and `turn.failed`. What could NOT be
confirmed is which event carries assistant text, because this machine's Codex
is not signed in and every turn 401s before producing any.

So text extraction is deliberately structural rather than keyed to one event
name: it walks whatever item or delta arrives and takes the text it finds. That
is uglier than the Claude adapter, and it is the honest shape for a protocol
observed only at its edges. When a signed-in Codex is available, watch one real
turn and tighten this.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Iterator

from .base import SPOKEN_STYLE, Adapter, Chunk, installed

# Keys that have carried assistant text in this family of protocols. Checked
# in order; the first non-empty string wins.
_TEXT_KEYS = ("text", "delta", "content", "message")


def _harvest_text(node: Any, depth: int = 0) -> str:
    """Pull assistant text out of an item of unknown shape."""
    if depth > 4:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_harvest_text(x, depth + 1) for x in node)
    if isinstance(node, dict):
        for key in _TEXT_KEYS:
            if key in node:
                found = _harvest_text(node[key], depth + 1)
                if found:
                    return found
    return ""


class Codex(Adapter):
    name = "codex"

    # Codex's own spelling of "do not stop to ask", as used by omarchy-agent.
    def __init__(self, model: str | None = None, spoken: bool = True,
                 cwd: str | None = None, full_auto: bool | None = None,
                 ask_permission: bool = True):
        self.model = model
        self.spoken = spoken
        self.cwd = cwd
        # --approve-for-me is Codex's unattended mode. Without it Codex uses
        # its own default, which asks -- and with no terminal to ask in, a
        # turn that needs approval stalls until the adapter is cancelled.
        # That fails closed, which is the right direction, but it is a guess:
        # this machine's Codex is not signed in and it has never been run.
        self.full_auto = (not ask_permission) if full_auto is None else full_auto
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        """Installed AND signed in.

        An unauthenticated Codex reaches `thread.started` and then 401s five
        times per transport before failing the turn, so it would narrate an
        auth error rather than answer. Absent is the more useful verdict:
        the loop falls back to echoing instead.
        """
        if not installed("codex"):
            return False
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        return (home / "auth.json").exists()

    def _argv(self, text: str, session_id: str | None) -> list[str]:
        argv = ["codex", "exec", "--json", "--skip-git-repo-check"]
        if session_id:
            argv += ["resume", session_id]
        if self.model:
            argv += ["-m", self.model]
        if self.full_auto:
            argv.append("--approve-for-me")
        prompt = f"{SPOKEN_STYLE}\n\n{text}" if self.spoken else text
        argv.append(prompt)
        return argv

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        argv = self._argv(text, session_id)
        try:
            # stdin must be closed: `codex exec` otherwise waits on it
            # ("Reading additional input from stdin...") and never returns.
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start codex: {e}")
            return

        with self._lock:
            self._proc = proc

        spoken_any = False
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue          # codex interleaves plain log lines
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                kind = msg.get("type", "")

                if kind == "thread.started":
                    yield Chunk(session_id=msg.get("thread_id"))

                elif kind == "error":
                    # Retry chatter is noisy but not terminal; only turn.failed
                    # ends the turn.
                    continue

                elif kind == "turn.failed":
                    err = (msg.get("error") or {}).get("message") or "codex turn failed"
                    yield Chunk(error=str(err))

                elif kind.startswith("item."):
                    item = msg.get("item") or msg
                    itype = str(item.get("type", ""))
                    if itype == "error":
                        continue
                    if "tool" in itype or "command" in itype:
                        yield Chunk(tool=item.get("name") or itype)
                        continue
                    found = _harvest_text(item)
                    if found.strip():
                        spoken_any = True
                        yield Chunk(text=found)

                elif kind == "turn.completed":
                    pass

            code = proc.wait()
            if code != 0 and not spoken_any and code not in (-15, 143, -9, 137):
                err = (proc.stderr.read() or "").strip().splitlines()
                yield Chunk(error=(err[-1] if err else f"codex exited {code}"))
        finally:
            with self._lock:
                self._proc = None
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

        yield Chunk(done=True)

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
