"""Gemini CLI, headless.

Deliberately uses plain text output rather than `-o stream-json`: guessing an
event schema produces an adapter that looks right and silently yields nothing,
which is the worst failure mode available. Plain stdout has no schema to get
wrong. The cost is losing tool-call visibility and token counts.

Watched against a live turn on 2026-09-22, which corrected three guesses:

  - auth lives at security.auth.selectedType, not the selectedAuthType this
    used to grep for, so a configured Gemini was reported as absent;
  - headless runs refuse outright in a folder the CLI has not been told to
    trust, and the project directory is the user's to choose, so it will
    usually be one Gemini has never seen;
  - the CLI prints "Warning: 256-color support not detected" on stdout, which
    this would have handed to the speaker and said out loud.

Google discontinued Gemini CLI for individual *Code Assist* accounts in June
2026 (IneligibleTierError, UNSUPPORTED_CLIENT) and points them at Antigravity.
An AI Studio API key still drives this CLI -- that is a different product from
the OAuth login that was withdrawn -- so the check below accepts a key and
refuses the login path rather than reporting a backend that 401s every turn.

Also learned the hard way: `--approval-mode yolo` is silently downgraded --
"Approval mode overridden to default because the current folder is not
trusted" -- so an untrusted directory will hang waiting for approval that
nobody can give. The adapter passes the flag and the caller has to trust the
folder for it to mean anything.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
from typing import Iterator

from .base import Adapter, Chunk, SPOKEN_STYLE, Watchdog, installed, stop_tree


class Gemini(Adapter):
    name = "gemini"

    def __init__(self, model: str | None = None, spoken: bool = True,
                 cwd: str | None = None, approval_mode: str | None = None,
                 ask_permission: bool = True):
        self.model = model
        self.spoken = spoken
        self.cwd = cwd
        # There is no way to prompt from here, so asking for permission means
        # choosing the mode that cannot act: `plan` is Gemini's documented
        # read-only mode. `yolo` is the unattended one and is only used when
        # the user has explicitly turned the guard off.
        self.approval_mode = approval_mode or ("plan" if ask_permission else "yolo")
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    #: Withdrawn for individuals in June 2026. Present in settings long after
    #: it stopped working, so treating it as usable means narrating an auth
    #: error once per sentence -- exactly what available() exists to prevent.
    DEAD_AUTH = {"oauth-personal", "LOGIN_WITH_GOOGLE"}

    def available(self) -> bool:
        if not installed("gemini"):
            return False
        # An unauthenticated Gemini fails on every turn, so treat it as absent
        # rather than as a backend that errors once per sentence.
        if any(os.environ.get(k) for k in
               ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA")):
            return True
        settings = os.path.expanduser("~/.gemini/settings.json")
        try:
            with open(settings) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return False
        auth = ((data.get("security") or {}).get("auth") or {})
        chosen = auth.get("selectedType") or data.get("selectedAuthType")
        return bool(chosen) and chosen not in self.DEAD_AUTH

    def why_unavailable(self) -> str:
        if not installed("gemini"):
            return "gemini is not installed"
        settings = os.path.expanduser("~/.gemini/settings.json")
        try:
            with open(settings) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return "gemini has no auth configured — run: gemini"
        auth = ((data.get("security") or {}).get("auth") or {})
        chosen = auth.get("selectedType") or data.get("selectedAuthType")
        if chosen in self.DEAD_AUTH:
            # The single most likely reason today, and the one nobody would
            # guess from a silent fallback to echoing.
            return ("Google withdrew this Gemini login in June 2026 — "
                    "use an API key, or switch to Antigravity")
        return "gemini has no auth configured — run: gemini"

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        prompt = f"{SPOKEN_STYLE}\n\n{text}" if self.spoken else text
        argv = ["gemini", "-p", prompt, "--approval-mode", self.approval_mode]
        if self.model:
            argv += ["-m", self.model]

        # Headless Gemini refuses to run in a folder it has not been told to
        # trust, and the project directory is chosen by the user, so it will
        # usually be one it has never seen. Trust is asserted here and safety
        # is carried by --approval-mode, which is the flag the caller actually
        # controls; without this the turn does not start at all.
        env = {**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL, start_new_session=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1, env=env)
        except OSError as e:
            yield Chunk(error=f"could not start gemini: {e}")
            return

        with self._lock:
            self._proc = proc
        dog = Watchdog(proc, self.idle_timeout_s)

        got_text = False
        try:
            # Gemini prints prose, and prefixes its own notices. Those are
            # dropped rather than spoken.
            for line in proc.stdout:
                dog.poke()
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith(("YOLO mode", "Approval mode", "Loaded cached",
                                        "Data collection", "Flushing", "Warning:",
                                        "Warning ")):
                    continue
                got_text = True
                yield Chunk(text=line)
            code = proc.wait()
            if dog.fired:
                # Killed for going quiet. Indistinguishable from a cancel by
                # exit code alone, and silence is the one thing a voice
                # interface must never answer with.
                yield Chunk(error=f"gemini stopped responding after "
                                  f"{dog.idle_s:.0f}s")
                return
            if code != 0 and not got_text and code not in (-15, 143, -9, 137):
                err = (proc.stderr.read() or "").strip().splitlines()
                yield Chunk(error=(err[-1] if err else f"gemini exited {code}"))
        finally:
            dog.stop()
            with self._lock:
                self._proc = None
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass

        # Gemini's headless mode is one-shot: no session handle to carry, so
        # each turn starts clean. Continuity would need the OpenAI-compatible
        # adapter against a Gemini endpoint instead.
        yield Chunk(done=True)

    def cancel(self) -> None:
        with self._lock:
            proc = self._proc
        if proc and proc.poll() is None:
            stop_tree(proc, signal.SIGTERM)
