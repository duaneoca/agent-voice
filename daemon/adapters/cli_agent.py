"""The other nine agents Omarchy knows, as plain-text CLI subprocesses.

None of these is installed on this machine, so none of their headless
protocols could be observed. Rather than ship nine speculative JSON parsers,
this is one adapter with a command template per agent and plain stdout
streaming -- the lowest-common-denominator contract that every one of them
supports.

The templates come from `omarchy-agent`, which already encodes each CLI's
spelling of "do not stop and ask". Where that invocation is interactive (most
of them launch a TUI), the template here uses the CLI's documented
non-interactive form instead and is marked UNVERIFIED: it is a starting point
for someone with that agent installed, not a tested path.

Verified-by-observation adapters live in their own modules: claude_code.py
(fully), codex.py (envelope only), gemini.py (flags only).
"""
from __future__ import annotations

import shlex
import shutil
import signal
import subprocess
import threading
from typing import Iterator

from .base import Adapter, Chunk, SPOKEN_STYLE, Watchdog, installed, stop_tree

#: agent id -> (argv template, verified?). "{prompt}" is substituted.
#: Confirmed non-interactive by omarchy-agent's own table:
#:   crush -- `crush run "<prompt>"` never prompts.
#: Everything else is the CLI's documented headless form, untested here.
TEMPLATES: dict[str, tuple[str, bool]] = {
    "crush":        ("crush run {prompt}", True),
    "opencode":     ("opencode run --auto {prompt}", False),
    "copilot":      ("copilot --allow-all -p {prompt}", False),
    "grok":         ("grok --permission-mode bypassPermissions -p {prompt}", False),
    "cursor-agent": ("cursor-agent --yolo --trust -p {prompt}", False),
    "muse":         ("muse --approval-mode never -p {prompt}", False),
    "omp":          ("omp --auto-approve -p {prompt}", False),
    "pi":           ("pi {prompt}", False),
    # Hermes has an HTTP API and hermes-satellite already talks to it; prefer
    # OpenAICompatible for it. This is the CLI fallback.
    "hermes":       ("hermes chat --yolo --query={prompt}", False),
    "openclaw":     ("openclaw run --message {prompt}", False),
}


class CliAgent(Adapter):
    """Runs one configured command per turn and streams its stdout."""

    #: The flag each template uses to skip approvals. Removed when the user
    #: has asked to be consulted, since none of these can consult anyone.
    BYPASS_FLAGS = ("--allow-all", "--yolo", "--trust", "--auto-approve",
                    "--auto", "--permission-mode", "bypassPermissions")

    def __init__(self, agent: str, spoken: bool = True, cwd: str | None = None,
                 ask_permission: bool = True):
        template, verified = TEMPLATES[agent]
        self.name = agent
        self.template = template
        self.verified = verified
        self.spoken = spoken
        self.cwd = cwd
        self.ask_permission = ask_permission
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    @property
    def binary(self) -> str:
        return shlex.split(self.template)[0]

    def available(self) -> bool:
        return installed(self.binary)

    def why_unavailable(self) -> str:
        # Omarchy puts a stub on PATH for every agent it knows, so "missing"
        # here usually means "never actually installed", not "not on PATH".
        return f"{self.binary} is not installed — run it once to install it"

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        prompt = f"{SPOKEN_STYLE}\n\n{text}" if self.spoken else text
        parts = shlex.split(self.template)
        if self.ask_permission:
            # None of these can raise a prompt, so the guard means refusing to
            # hand them a flag that skips one.
            parts = [p for p in parts if p not in self.BYPASS_FLAGS]
        argv = [prompt if part == "{prompt}" else part.replace("{prompt}", prompt)
                for part in parts]
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL, start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start {self.binary}: {e}")
            return

        with self._lock:
            self._proc = proc
        dog = Watchdog(proc, self.idle_timeout_s)

        got_text = False
        try:
            for line in proc.stdout:
                dog.poke()
                if line.strip():
                    got_text = True
                    yield Chunk(text=line)
            code = proc.wait()
            if dog.fired:
                # Killed for going quiet. Indistinguishable from a cancel by
                # exit code alone, and silence is the one thing a voice
                # interface must never answer with.
                yield Chunk(error=f"{self.binary} stopped responding after "
                                  f"{dog.idle_s:.0f}s")
                return
            if code != 0 and not got_text and code not in (-15, 143, -9, 137):
                err = (proc.stderr.read() or "").strip().splitlines()
                yield Chunk(error=(err[-1] if err else f"{self.binary} exited {code}"))
        finally:
            dog.stop()
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
            stop_tree(proc, signal.SIGTERM)
