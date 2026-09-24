"""Backend selection.

Omarchy already answers "which agent" -- one line in
~/.config/omarchy/defaults/agent, set by `omarchy default agent <name>`, and
thirteen agents are spelled there. Reading it means switching agents at the
desktop level switches the voice too, with nothing to configure twice.
"""
from __future__ import annotations

from pathlib import Path

from .base import Adapter, Chunk, sentences, speech_safe
from .cli_agent import TEMPLATES as CLI_TEMPLATES, CliAgent
from .antigravity import Antigravity
from .claude_code import ClaudeCode
from .codex import Codex
from .gemini import Gemini
from .openai_compat import OpenAICompatible

DEFAULT_AGENT_FILE = Path.home() / ".config/omarchy/defaults/agent"

# Omarchy's spelling -> our adapter, for the four that have a module of their
# own. How far each was verified is in its docstring; claude, codex, gemini
# and agy have all answered a live turn.
#
# "agy" is not one of Omarchy's own agent names -- `omarchy default agent` has
# a hardcoded list and Antigravity is not on it yet -- so selecting it means
# writing the defaults file directly until that changes. Google is migrating
# every individual Gemini CLI user here, so it is a question of when.
REGISTRY: dict[str, type[Adapter]] = {
    "claude": ClaudeCode,
    "codex": Codex,
    "gemini": Gemini,
    "agy": Antigravity,
    "antigravity": Antigravity,
    "antigravity-cli": Antigravity,
}


def omarchy_default() -> str | None:
    """Whatever `omarchy default agent` would print, or None when unset."""
    try:
        value = DEFAULT_AGENT_FILE.read_text().strip()
    except OSError:
        return None
    return value or None


def load(name: str | None = None, level: str | None = None,
         endpoint: dict | None = None, **kwargs) -> Adapter | None:
    """Build the adapter for `name`, or for Omarchy's default when omitted.

    Returns None when the agent is unset, unknown, or installed-but-unusable
    (an unauthenticated CLI counts as unusable -- better to fall back to echo
    than to narrate an auth error once per sentence).
    """
    # A configured endpoint wins over the desktop's agent, because setting
    # one is a deliberate act and there is nowhere else to express it: Omarchy
    # has no entry for "the model on my other machine".
    if endpoint and endpoint.get("url") and endpoint.get("model"):
        adapter = OpenAICompatible(
            base_url=endpoint["url"], model=endpoint["model"],
            api_key=endpoint.get("key") or None,
            is_agent=bool(endpoint.get("is_agent")),
            spoken=kwargs.get("spoken", True))
        return adapter if adapter.available() else None

    agent = name or omarchy_default()
    if not agent:
        return None

    def finish(adapter: Adapter) -> Adapter | None:
        # Applied after construction so every adapter takes the same call,
        # whether or not it can do anything with the level -- but only when
        # the backend says it can honour it. A level stored before a backend
        # withdrew support for it must not keep taking effect: agy dropped
        # "edits" once it turned out not to confine writes to the project,
        # and a stale value would otherwise have gone on selecting the very
        # mode that was withdrawn.
        if level and level in getattr(adapter, "levels", ()):
            adapter.level = level
        return adapter if adapter.available() else None

    cls = REGISTRY.get(agent)
    if cls is not None:
        return finish(cls(**kwargs))

    # The remaining nine Omarchy agents share one plain-stdout adapter.
    if agent in CLI_TEMPLATES:
        return finish(CliAgent(agent, **kwargs))

    return None


def explain(name: str | None = None) -> str:
    """Why there is no adapter for `name`, in words worth putting on screen.

    `load()` answers None for four different reasons -- unset, unknown,
    missing, unauthenticated -- and a voice assistant that quietly starts
    echoing instead of answering looks broken rather than unconfigured. The
    likeliest cause today is that Google withdrew the Gemini login in June
    2026, which nobody would deduce from silence.
    """
    agent = name or omarchy_default()
    if not agent:
        return "no agent chosen — run: omarchy default agent claude"
    try:
        cls = REGISTRY.get(agent)
        adapter = cls() if cls is not None else (
            CliAgent(agent) if agent in CLI_TEMPLATES else None)
        if adapter is None:
            return f"{agent} is not an agent this knows about"
        # Asked about a backend that is actually fine, say nothing rather than
        # inventing a fault: this is also called to check, not only to explain.
        return "" if adapter.available() else adapter.why_unavailable()
    except Exception:
        return f"{agent} is unavailable"


def supported() -> dict[str, str]:
    """Every agent we can drive, and how well its protocol is known."""
    out = {"claude": "verified", "codex": "envelope only", "gemini": "flags only"}
    for agent, (_, verified) in CLI_TEMPLATES.items():
        out.setdefault(agent, "text stream" if verified else "UNVERIFIED")
    return out


__all__ = ["Adapter", "Chunk", "ClaudeCode", "CliAgent", "Codex", "Gemini",
           "OpenAICompatible", "load", "omarchy_default", "sentences",
           "speech_safe", "supported", "REGISTRY", "CLI_TEMPLATES"]
