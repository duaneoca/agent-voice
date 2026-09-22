"""Make agent replies speakable, and chunk them for streaming TTS.

Adapted from duaneoca/hermes-satellite (src/hermes_satellite/core/speech_text.py),
which had already solved this properly. Kept nearly verbatim because the edge
cases here are all ones that only show up when you listen to the output:

  - a sentence boundary must not split "2.47 PM" or "J. Smith"
  - fragments under ~20 characters make TTS sound choppy, so they merge forward
  - a fenced code block becomes "Code omitted", not silence
  - list items need terminal punctuation or they run together when joined

The system prompt asking for plain prose is the first line of defence; this is
the second, for whatever slips through anyway.
"""
from __future__ import annotations

import re

_CODE_BLOCK = re.compile(r"```.*?(```|\Z)", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*|__([^_]+)__")
_ITALIC = re.compile(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])|(?<![\w_])_([^_\n]+)_(?![\w_])")
_HEADER = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+", re.MULTILINE)
_BLOCKQUOTE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)
_BULLET = re.compile(r"^[ \t]*(?:[-*•+]|\d+[.)])[ \t]+")
_EMOJI = re.compile(
    "["
    "\U0001f000-\U0001faff"
    "←-⇿"
    "⌀-➿"
    "⬀-⯿"
    "️"
    "]+"
)
_WHITESPACE = re.compile(r"[ \t]+")
_SENTENCE_END = (".", "!", "?", ":", ";", ",")


def make_speakable(text: str) -> str:
    """Flatten markdown-ish agent output into plain prose for TTS."""
    if not text:
        return ""

    text = _CODE_BLOCK.sub(" Code omitted. ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _IMAGE.sub(r"\1", text)
    text = _LINK.sub(r"\1", text)
    text = _BOLD.sub(lambda m: m.group(1) or m.group(2), text)
    text = _ITALIC.sub(lambda m: m.group(1) or m.group(2), text)
    text = _HEADER.sub("", text)
    text = _BLOCKQUOTE.sub("", text)

    lines = []
    for line in text.splitlines():
        stripped = _BULLET.sub("", line)
        if stripped != line:
            stripped = stripped.rstrip()
            if stripped and not stripped.endswith(_SENTENCE_END):
                stripped += "."
        lines.append(stripped)
    text = " ".join(line.strip() for line in lines if line.strip())

    text = text.replace("|", " ").replace("*", " ").replace("#", " ")
    text = _EMOJI.sub("", text)
    return _WHITESPACE.sub(" ", text).strip()


# A boundary is .!? followed by whitespace -- but not inside a decimal number
# ("2.47 PM") and not after a single capital initial ("J. Smith").
_BOUNDARY = re.compile(r"(?<!\d)(?<![A-Z])[.!?]+(?=\s)")

#: Sentences shorter than this merge forward so TTS does not sound choppy.
MIN_CHUNK_CHARS = 20


def iter_sentences(deltas):
    """Group a stream of text deltas into speakable sentence chunks."""
    buffer = ""
    pending = ""
    for delta in deltas:
        buffer += delta
        while True:
            match = _BOUNDARY.search(buffer)
            if not match:
                break
            sentence = buffer[:match.end()]
            buffer = buffer[match.end():].lstrip()
            candidate = (pending + " " + sentence).strip() if pending else sentence.strip()
            if len(candidate) < MIN_CHUNK_CHARS:
                pending = candidate
                continue
            pending = ""
            yield candidate
    tail = (pending + " " + buffer).strip() if pending else buffer.strip()
    if tail:
        yield tail


# Spoken commands that mean "stop": exact-match against the normalized
# transcript, so an ordinary sentence merely containing "stop" cannot misfire.
_STOP_PHRASES = frozenset({
    "stop", "stop talking", "stop it", "cancel", "cancel that", "never mind",
    "nevermind", "be quiet", "shut up", "thats all", "forget it", "abort",
})


def is_stop_command(text: str) -> bool:
    """True when the transcript is a stop command and nothing else."""
    norm = re.sub(r"[^a-z ]+", " ", text.lower().replace("'", ""))
    return " ".join(norm.split()) in _STOP_PHRASES
