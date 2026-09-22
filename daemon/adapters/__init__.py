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
from .claude_code import ClaudeCode
from .codex import Codex
from .gemini import Gemini
from .openai_compat import OpenAICompatible

DEFAULT_AGENT_FILE = Path.home() / ".config/omarchy/defaults/agent"

# Omarchy's spelling -> our adapter, for the three that have a module of their
# own. How far each was verified is in its docstring: claude fully, codex's
# envelope only, gemini's flags only.
REGISTRY: dict[str, type[Adapter]] = {
    "claude": ClaudeCode,
    "codex": Codex,
    "gemini": Gemini,
}


def omarchy_default() -> str | None:
    """Whatever `omarchy default agent` would print, or None when unset."""
    try:
        value = DEFAULT_AGENT_FILE.read_text().strip()
    except OSError:
        return None
    return value or None


def load(name: str | None = None, **kwargs) -> Adapter | None:
    """Build the adapter for `name`, or for Omarchy's default when omitted.

    Returns None when the agent is unset, unknown, or installed-but-unusable
    (an unauthenticated CLI counts as unusable -- better to fall back to echo
    than to narrate an auth error once per sentence).
    """
    agent = name or omarchy_default()
    if not agent:
        return None

    cls = REGISTRY.get(agent)
    if cls is not None:
        adapter = cls(**kwargs)
        return adapter if adapter.available() else None

    # The remaining nine Omarchy agents share one plain-stdout adapter.
    if agent in CLI_TEMPLATES:
        adapter = CliAgent(agent, **kwargs)
        return adapter if adapter.available() else None

    return None


def supported() -> dict[str, str]:
    """Every agent we can drive, and how well its protocol is known."""
    out = {"claude": "verified", "codex": "envelope only", "gemini": "flags only"}
    for agent, (_, verified) in CLI_TEMPLATES.items():
        out.setdefault(agent, "text stream" if verified else "UNVERIFIED")
    return out


__all__ = ["Adapter", "Chunk", "ClaudeCode", "CliAgent", "Codex", "Gemini",
           "OpenAICompatible", "load", "omarchy_default", "sentences",
           "speech_safe", "supported", "REGISTRY", "CLI_TEMPLATES"]
