"""The one contract every backend implements.

Take text plus a session id, stream text back. Nothing in here knows about
audio, and nothing in the voice loop knows which agent answered.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator


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
