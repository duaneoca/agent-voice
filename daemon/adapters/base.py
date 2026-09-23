"""The one contract every backend implements.

Take text plus a session id, stream text back. Nothing in here knows about
audio, and nothing in the voice loop knows which agent answered.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


#: Seconds of complete silence from a backend before it is presumed hung.
#: Idle, not total: an agent that is still streaming is still working, and a
#: real task can legitimately run for minutes. What must not happen is the
#: loop waiting forever on something that will never speak -- Gemini answers
#: a 503 by retrying with backoff rather than failing, so a turn can block
#: indefinitely while the panel sits on "thinking" and the room stays silent.
IDLE_TIMEOUT_S = 90.0


class Watchdog:
    """Kills a subprocess that has gone quiet for too long.

    Poked on every line received. A backend that is streaming never trips it;
    one that has stopped speaking is killed, its pipes close, and the read
    loop ends normally so the adapter can report the failure out loud instead
    of leaving the turn hanging with nothing on screen and nothing in the air.
    """

    def __init__(self, proc, idle_s: float = IDLE_TIMEOUT_S) -> None:
        self.idle_s = idle_s
        self.fired = False
        self._proc = proc
        self._last = time.monotonic()
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def poke(self) -> None:
        self._last = time.monotonic()

    def stop(self) -> None:
        self._done.set()

    def _watch(self) -> None:
        while not self._done.wait(0.5):
            if self._proc.poll() is not None:
                return
            if time.monotonic() - self._last >= self.idle_s:
                self.fired = True
                try:
                    self._proc.kill()
                except Exception:
                    pass
                return


def installed(binary: str) -> bool:
    """True only when the command is really there, not a cold stub.

    Omarchy puts a stub on PATH for every agent it knows from first boot:

        #!/bin/bash
        mise use -g "crush" || exit 1
        exec mise x "crush" -- "crush" "$@"

    So `which` finds all thirteen on a machine with none of them installed,
    and invoking one kicks off a minute-long install. Omarchy's own
    `omarchy-cmd-present` is just `command -v` and has the same blind spot --
    which is why `omarchy-default-agent` pointedly does not use it for Hermes.

    A voice loop must not trigger an install mid-sentence, so a stub counts as
    absent until mise actually has the tool.
    """
    import os
    import shutil

    path = shutil.which(binary)
    if not path:
        return False
    try:
        if os.path.getsize(path) < 4096:
            with open(path, "r", errors="replace") as handle:
                if "mise use -g" in handle.read():
                    root = os.path.expanduser("~/.local/share/mise/installs")
                    return os.path.isdir(os.path.join(root, binary))
    except OSError:
        pass
    return True


@dataclass
class Chunk:
    """One thing that happened while the agent was answering.

    A chunk carries at most one kind of news. `text` arrives many times,
    everything else at most once per turn.
    """
    text: str = ""
    session_id: str | None = None
    ttft_ms: float | None = None
    tool: str | None = None          # a tool the agent decided to run
    done: bool = False
    error: str | None = None
    cost_usd: float | None = None
    duration_ms: float | None = None


class Adapter(ABC):
    """A backend that can hold a conversation."""

    #: Name as Omarchy's `default agent` spells it.
    name: str = ""

    #: True only when the adapter can actually put a permission prompt on
    #: screen and wait for an answer. Claude Code can, through a PreToolUse
    #: hook. Nothing else here can, so the honest thing is to say so rather
    #: than let a setting called "ask before it changes anything" quietly mean
    #: nothing. An adapter that cannot guard runs in the safest mode its CLI
    #: offers instead of its unattended one.
    guards_permissions: bool = False

    #: The permission levels this backend can actually honour, in order.
    #: "edits" means files inside the project may be changed without asking
    #: while commands still are -- a distinction that needs either a hook we
    #: can answer or a flag the CLI provides. Most have neither, so for them
    #: "ask" and "edits" would be the same posture, and offering both would
    #: be a setting that changes nothing. Two honest options beat three where
    #: one is a lie.
    levels: tuple[str, ...] = ("ask", "trusted")

    #: Seconds of silence before this backend is presumed hung.
    idle_timeout_s: float = IDLE_TIMEOUT_S

    #: The chosen permission level. Most backends can only act on the coarse
    #: ask_permission flag; the ones that can tell "edits" apart read this.
    level: str = "ask"

    def why_unavailable(self) -> str:
        """Why `available()` said no, in words the panel can show.

        An agent that vanishes with no account of itself reads as a broken
        voice assistant rather than as a backend that needs attention -- and
        the most likely reason today is that Google withdrew a login, which
        nobody would guess from silence.
        """
        return f"{self.name} is not installed"

    def posture(self, level: str) -> str:
        """One line naming what `level` really means for this backend.

        Shown on the panel, because the whole point of per-agent levels is
        that the user can see what is in force rather than what was asked for.
        """
        if level == "trusted":
            return "trusted — never asks"
        if level == "edits" and "edits" in self.levels:
            return "edits here · commands ask"
        if self.guards_permissions:
            return "asks before every change"
        return f"read-only — {self.name} cannot ask"

    @abstractmethod
    def available(self) -> bool:
        """True when this backend can actually be run on this machine."""

    @abstractmethod
    def send(self, text: str, session_id: str | None = None) -> Iterator[Chunk]:
        """Stream the answer. Pass the previous turn's session_id to continue."""

    def cancel(self) -> None:
        """Abandon the turn in progress. Default: nothing to abandon."""


# Replies are spoken by a TTS voice, so they have to be written for the ear.
# Without this the agent answers in markdown and the voice reads the asterisks
# and backticks out loud.
SPOKEN_STYLE = (
    "Your reply is being read aloud by a text-to-speech voice, so write for "
    "the ear, not the screen. No markdown of any kind: no asterisks, bold, "
    "backticks, code fences, bullet lists, numbered lists, headings, tables "
    "or emoji. No URLs. Mention file paths and commands only when they are "
    "the point, and say them in words a listener can follow. Use short plain "
    "sentences. Keep the whole answer to two or three sentences unless asked "
    "for detail, and lead with the answer rather than the reasoning. If you "
    "must list things, say them in a sentence joined by 'and'."
)

def speech_safe(text: str) -> str:
    """Flatten markdown for TTS. See daemon/speech_text.make_speakable."""
    from speech_text import make_speakable
    return make_speakable(text)


def sentences(chunks: Iterator["Chunk"]) -> Iterator[str]:
    """Regroup a Chunk stream into speakable sentences.

    Speech has to start before the agent has finished writing, or the pause
    after you stop talking is the agent's entire response time. Piper renders
    a whole utterance before yielding audio, so the split happens here: hand
    it one sentence at a time and the first is speakable while the rest is
    still being generated.

    The chunking itself is speech_text.iter_sentences, which knows not to
    split "2.47 PM" or "J. Smith" and merges fragments too short to speak
    without sounding choppy.
    """
    from speech_text import iter_sentences

    def deltas():
        for chunk in chunks:
            if chunk.error:
                return
            if chunk.text:
                yield chunk.text

    yield from iter_sentences(deltas())
