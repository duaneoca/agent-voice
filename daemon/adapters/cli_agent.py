"""The other nine agents Omarchy knows, as plain-text CLI subprocesses.

None of the nine was installed where this was written, so none of their
headless protocols has been observed. Rather than ship nine speculative
JSON parsers,
this is one adapter with a command template per agent and plain stdout
streaming -- the lowest-common-denominator contract that every one of them
supports.

The templates come from `omarchy-agent`, which already encodes each CLI's
spelling of "do not stop and ask". Where that invocation is interactive (most
of them launch a TUI), the template here uses the CLI's documented
non-interactive form instead and is marked UNVERIFIED: it is a starting point
for someone with that agent installed, not a tested path.

Adapters that have answered a real turn live in their own modules:
claude_code.py, codex.py, gemini.py and antigravity.py. Nothing on this
shared template has, so what each template claims about "do not stop and
ask" is a reading of a --help page, not an observation.
"""
from __future__ import annotations

import shlex
import signal
import subprocess
import threading
from typing import Iterator

from .base import (
    Adapter, Chunk, Drain, SPOKEN_STYLE, Watchdog, feed, installed,
    stop_tree,
)

#: agent id -> (argv template, verified?). "{prompt}" is substituted.
#: Confirmed non-interactive by omarchy-agent's own table:
#:   crush -- `crush run "<prompt>"` never prompts.
#: Everything else is the CLI's documented headless form, untested here.
#:
#: A template with no "{prompt}" is one whose CLI takes the prompt on stdin,
#: which keeps spoken text out of argv -- /proc/<pid>/cmdline is world
#: readable on an ordinary Linux box, so anything passed as an argument can
#: be read by any local account while the turn runs. Opt-in per CLI and not
#: assumed: a prompt written to a channel the CLI is not reading is a turn
#: that hangs until the watchdog kills it. Of these ten, only copilot
#: documents piped stdin and only copilot has been shown to accept it, so the
#: other nine still expose the prompt in argv. That is a known limitation
#: rather than a decision, and each one closes as somebody runs a real turn
#: against it.
TEMPLATES: dict[str, tuple[str, bool]] = {
    "crush":        ("crush run {prompt}", True),
    "opencode":     ("opencode run --auto {prompt}", False),
    # No {prompt}: copilot reads it from piped stdin, which its --help
    # documents ("combine with -i, -p, or piped stdin") and a real turn
    # confirms -- it is the only one of these ten that does. The rest still
    # pass the prompt in argv; see the note above TEMPLATES.
    "copilot":      ("copilot --allow-all", True),
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
        # A template with no {prompt} means "this CLI reads it on stdin",
        # which keeps spoken text out of argv -- world readable through
        # /proc/<pid>/cmdline on an ordinary Linux box. Opt-in per CLI rather
        # than assumed for all ten: a prompt sent down a channel a CLI is not
        # reading is a turn that hangs until the watchdog kills it, and most
        # of these have never answered a turn here to prove either way.
        on_stdin = "{prompt}" not in self.template
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, start_new_session=True,
                                    stdin=(subprocess.PIPE if on_stdin
                                           else subprocess.DEVNULL),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start {self.binary}: {e}")
            return
        if on_stdin:
            feed(proc, prompt)

        # Drained in the background from here on: stderr is a 64KB pipe
        # and it is not read until stdout ends, so a chatty CLI fills it,
        # blocks on its next write, and stops producing stdout -- which the
        # watchdog then reports as "stopped responding".
        errors = Drain(proc.stderr)

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
                err = errors.text().splitlines()
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
