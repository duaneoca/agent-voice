"""Antigravity CLI (`agy`), headless, via `--output-format json`.

Google withdrew Gemini CLI for individual Code Assist accounts in June 2026
and pointed them here. Antigravity's CLI is included at every tier including
free, on a personal Google account, which is the arrangement an Omarchy user
is likely to have -- where Gemini now wants a billed API key.

Verified against agy 1.2.8 on 2026-09-23. A turn is one JSON object:

    {"conversation_id": "34acf3c8-...", "status": "SUCCESS",
     "response": "agentvoice ok\\n", "duration_seconds": 3.4,
     "num_turns": 1, "usage": {...}}

Two things about that envelope decide how this is written.

It is emitted on failure too, with `status` set to ERROR and a populated
`error` -- and **the process still exits 0**. An adapter that trusted the
exit code would report an authentication failure as a successful empty
answer, which in a voice interface is silence. `status` is the contract
here; the exit code carries no information.

`conversation_id` is returned in every output format and `--conversation`
resumes it, so conversation mode works without the process staying alive.

"edits" is *not* offered here, and `levels` is the authority on that. The
pieces look as though it could be: `--mode accept-edits` and `--add-dir`
describe a scope declaratively, ahead of time, which is roughly what the
level means. What is missing is the confinement -- there is no flag that
restricts what is reachable outside the added directory, and a hook cannot
supply one either, because in headless mode `allow` is ignored, so a hook
there can refuse and never permit (upstream issue #1053). Offering the level
under a name that means confinement on the same screen would import a
guarantee that is not here. See the note above `levels`.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from typing import Iterator

from .base import (
    Adapter, Chunk, Drain, SPOKEN_STYLE, Watchdog, installed, stop_tree,
)


class Antigravity(Adapter):
    name = "agy"
    remembers = True          # --conversation <id>, verified across two turns
    # No hook that can grant, so nothing can be asked mid-call.
    guards_permissions = False
    # "edits" is deliberately absent, and it was offered here until it was
    # tested. Our "edits" means files *inside the project* change without
    # asking. `--mode accept-edits --add-dir <project>` does not mean that:
    # asked to write outside the project it did so and reported SUCCESS, both
    # with the default settings and with allowNonWorkspaceAccess turned off.
    # --add-dir adds to a workspace rather than restricting to one, and there
    # is no flag that restricts. Offering the level under a name that means
    # confinement elsewhere would import a guarantee that is not here; a user
    # would read it as the Claude Code behaviour, because that is what it says
    # on the same screen. It can come back when confinement can be shown.
    levels = ("ask", "trusted")

    def __init__(self, model: str | None = None, cwd: str | None = None,
                 spoken: bool = True, ask_permission: bool = True,
                 level: str = "ask"):
        self.model = model
        self.spoken = spoken
        self.cwd = cwd or os.getcwd()
        self.ask_permission = ask_permission
        # ask_permission is the older two-state flag the loop still passes;
        # `level` refines it where the backend can tell the difference.
        self.level = level if level in self.levels else (
            "ask" if ask_permission else "trusted")
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return installed("agy")

    def why_unavailable(self) -> str:
        if not installed("agy"):
            return "agy is not installed — mise use -g agy"
        return "agy is not signed in — run: agy"

    def posture(self, level: str) -> str:
        if level == "trusted":
            return "trusted — never asks, not confined to the project"
        return "read-only — agy plans but does not act"

    def _argv(self, text: str, session_id: str | None) -> list[str]:
        prompt = f"{SPOKEN_STYLE}\n\n{text}" if self.spoken else text
        argv = ["agy", "-p", prompt, "--output-format", "json",
                # The workspace is the project the user chose; without this the
                # agent's idea of "here" is wherever the daemon happens to be.
                "--add-dir", self.cwd,
                # Bounded so a wedged turn cannot outlive the watchdog and
                # leave an orphan holding the microphone's attention.
                #
                # Worth being clear about what this costs: agy's
                # --print-timeout is a *total* limit, not an idle one, so
                # unlike every other backend here a long agy task is cut off
                # at 90s even while it is working. The alternative is an
                # orphan `agy` with no way to notice it, and the watchdog
                # cannot tell the difference from out here -- agy emits one
                # JSON document at the end rather than streaming, so there is
                # nothing to poke the watchdog with in between. Raise
                # idle_timeout_s if agy is the backend doing real work.
                "--print-timeout", f"{int(self.idle_timeout_s)}s"]
        if self.level == "trusted":
            argv.append("--dangerously-skip-permissions")
        else:
            # accept-edits only if this adapter still claims to support it.
            # It does not today, so this is the second lock on the same door.
            accepts = self.level == "edits" and "edits" in self.levels
            argv += ["--mode", "accept-edits" if accepts else "plan"]
        if self.model:
            argv += ["--model", self.model]
        if session_id:
            argv += ["--conversation", session_id]
        return argv

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        argv = self._argv(text, session_id)
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL, start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start agy: {e}")
            return

        # Drained in the background from here on: stderr is a 64KB pipe
        # and it is not read until stdout ends, so a chatty CLI fills it,
        # blocks on its next write, and stops producing stdout -- which the
        # watchdog then reports as "stopped responding".
        Drain(proc.stderr)

        with self._lock:
            self._proc = proc
        dog = Watchdog(proc, self.idle_timeout_s)

        payload: dict = {}
        try:
            # The envelope is one object, but it is not necessarily the only
            # line: diagnostics share stdout. Keep the last thing that parses
            # as an object carrying a status, and ignore the rest.
            for line in proc.stdout:
                dog.poke()
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    candidate = json.loads(line)
                except ValueError:
                    continue
                if isinstance(candidate, dict) and "status" in candidate:
                    payload = candidate
            proc.wait()
            if dog.fired:
                yield Chunk(error=f"agy stopped responding after {dog.idle_s:.0f}s")
                return
        finally:
            dog.stop()
            with self._lock:
                self._proc = None
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

        if not payload:
            yield Chunk(error="agy returned nothing this adapter could read")
            return

        conversation = str(payload.get("conversation_id") or "")
        if conversation:
            yield Chunk(session_id=conversation)

        # Never the exit code: agy exits 0 on authentication failure.
        if str(payload.get("status", "")).upper() != "SUCCESS":
            yield Chunk(error=str(payload.get("error") or "agy failed the turn"))
            return

        response = str(payload.get("response") or "").strip()
        if response:
            yield Chunk(text=response)
        yield Chunk(done=True)

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                stop_tree(proc, signal.SIGTERM)
            except Exception:
                pass
