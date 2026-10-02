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

#: C0 and C1 control characters, less tab and newline. Transcripts and model
#: replies are printed, and a terminal does not merely display these: OSC 52
#: writes the system clipboard in foot, and a CSI sequence can rewrite lines
#: that have already scrolled past. The daemon normally runs under systemd,
#: where they land in the journal instead -- but `agentvoice listen` in a
#: terminal is how it is developed and debugged, which is exactly when the
#: text being printed is the text nobody trusts.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe_for_terminal(text: str) -> str:
    """`text` with control characters taken out, for printing or logging."""
    return _CONTROL.sub("", str(text))


def as_prompt(text: str) -> str:
    """A transcript, in the shape it can be handed to a CLI as an argument.

    Every backend here passes the transcript in argv -- `claude -p <text>`,
    `codex exec <text>` -- where a leading hyphen is read as a flag and not
    as speech. Whisper does produce one: a pause, a dictated dash, or a false
    start comes back as "- something". The worst case is an unknown-flag
    error rather than anything running, but it is a turn lost to a character
    nobody said, and nothing a person says starts with a hyphen.
    """
    cleaned = _CONTROL.sub("", str(text)).strip()
    return cleaned.lstrip("-‐‑‒–— \t").strip()
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


# A boundary is .!? followed by whitespace -- but not after a single capital
# initial ("J. Smith"), which is what `\b[A-Z]` describes: a capital that is
# a word on its own.
#
# It used to be `(?<!\d)(?<![A-Z])`, which was too broad on both counts and
# refused to end a sentence after *any* digit or *any* capital: "shipped in
# 2024." and "fix the GPU." were never boundaries, so the rest of the reply
# merged into one long utterance. The decimal case it was guarding does not
# need a guard at all -- the "2.47" period is followed by a digit, and a
# boundary already has to be followed by whitespace.
_BOUNDARY = re.compile(r"(?<!\b[A-Z])[.!?]+(?=\s)")

#: Sentences shorter than this merge forward so TTS does not sound choppy.
MIN_CHUNK_CHARS = 20

#: A code fence. Matters here and not only in make_speakable, because the
#: splitting happens first: `make_speakable` turns a *whole* fenced block
#: into "Code omitted", but it is handed one sentence at a time, so a block
#: containing ". " used to be cut up -- the opening piece became "Code
#: omitted" and every piece after it was read out as code.
_FENCE = "```"


def _next_boundary(buffer: str):
    """The first sentence boundary outside a code fence, or None.

    Inside a fence nothing is a sentence, however much it looks like one: the
    whole block is going to become "Code omitted", and splitting it first
    means the opening piece becomes those two words and every piece after it
    is read out as code, one line at a time.

    A *closed* fence is skipped over, so a boundary after it is still found
    and the emitted chunk carries the complete block for make_speakable to
    flatten. An unclosed one ends the scan: the rest of it may still be on
    its way.
    """
    pos = 0
    while True:
        opens_at = buffer.find(_FENCE, pos)
        end = len(buffer) if opens_at < 0 else opens_at
        match = _BOUNDARY.search(buffer, pos, end)
        if match:
            return match
        if opens_at < 0:
            return None
        closes_at = buffer.find(_FENCE, opens_at + len(_FENCE))
        if closes_at < 0:
            return None
        pos = closes_at + len(_FENCE)


def iter_sentences(deltas):
    """Group a stream of text deltas into speakable sentence chunks."""
    buffer = ""
    pending = ""
    for delta in deltas:
        buffer += delta
        while True:
            match = _next_boundary(buffer)
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
