"""Gemini CLI, headless.

Deliberately uses plain text output rather than `-o stream-json`. The flags
were read off the installed CLI, but the stream-json event schema could not be
observed -- this machine's Gemini has no auth configured, so every invocation
stops at "Please set an Auth method". Guessing a JSON schema produces an
adapter that looks right and silently yields nothing, which is the worst
failure mode available. Plain stdout has no schema to get wrong.

The cost is losing tool-call visibility and token counts. Worth revisiting
once a signed-in Gemini can be watched for one turn.

Also learned the hard way: `--approval-mode yolo` is silently downgraded --
"Approval mode overridden to default because the current folder is not
trusted" -- so an untrusted directory will hang waiting for approval that
nobody can give. The adapter passes the flag and the caller has to trust the
folder for it to mean anything.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Iterator

from .base import SPOKEN_STYLE, Adapter, Chunk, installed


class Gemini(Adapter):
    name = "gemini"

    def __init__(self, model: str | None = None, spoken: bool = True,
                 cwd: str | None = None, approval_mode: str = "yolo"):
        self.model = model
        self.spoken = spoken
        self.cwd = cwd
        self.approval_mode = approval_mode
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

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
            return "selectedAuthType" in open(settings).read()
        except OSError:
            return False

    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        prompt = f"{SPOKEN_STYLE}\n\n{text}" if self.spoken else text
        argv = ["gemini", "-p", prompt, "--approval-mode", self.approval_mode]
        if self.model:
            argv += ["-m", self.model]

        try:
            proc = subprocess.Popen(argv, cwd=self.cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, bufsize=1)
        except OSError as e:
            yield Chunk(error=f"could not start gemini: {e}")
            return

        with self._lock:
            self._proc = proc

        got_text = False
        try:
            # Gemini prints prose, and prefixes its own notices. Those are
            # dropped rather than spoken.
            for line in proc.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith(("YOLO mode", "Approval mode", "Loaded cached",
                                        "Data collection", "Flushing")):
                    continue
                got_text = True
                yield Chunk(text=line)
            code = proc.wait()
            if code != 0 and not got_text and code not in (-15, 143, -9, 137):
                err = (proc.stderr.read() or "").strip().splitlines()
                yield Chunk(error=(err[-1] if err else f"gemini exited {code}"))
        finally:
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
            proc.terminate()
