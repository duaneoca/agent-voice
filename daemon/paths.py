"""Where everything lives.

One module so the daemon, the CLI and the training tool cannot disagree.

The repo root doubles as the plugin directory: `omarchy plugin add` clones
this repo straight into ~/.config/omarchy/plugins/duaneoca.agentvoice/, so
manifest.json and the QML sit at the top level and this package sits beside
them. Code is therefore found relative to this file, and never at an
absolute path.

Everything the installer downloads -- the interpreter, the wheels, the
models -- lives under XDG data instead, because it is large, machine
specific, and must survive `git pull` without showing up in `git status`.
"""
from __future__ import annotations

import os
from pathlib import Path

#: The repo / plugin directory: the parent of this package.
ROOT = Path(__file__).resolve().parent.parent

_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
_XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_XDG_RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")

#: Omarchy's shell config, where the bar widget's settings live. Derived from
#: XDG_CONFIG_HOME rather than hardcoding ~/.config, because a hardcoded home
#: is not redirectable: the permission tests read this file, and reading the
#: developer's real one made `decide()` answer according to whatever level was
#: set on the machine -- including "allow" to `rm -rf /` on a green run.
SHELL_JSON = _XDG_CONFIG / "omarchy/shell.json"

DATA_DIR = _XDG_DATA / "agentvoice"
CONFIG_DIR = _XDG_CONFIG / "agentvoice"
RUNTIME_DIR = _XDG_RUNTIME / "agentvoice"

VENV = DATA_DIR / "venv"
MODELS = DATA_DIR / "models"
VERIFIERS = DATA_DIR / "verifiers"
WAKEWORDS = DATA_DIR / "wakewords"

#: A development checkout keeps its models under bench/, so the benchmark and
#: the daemon can share one download. Installed copies have no bench/.
_DEV_MODELS = ROOT / "bench/models"


def models_dir() -> Path:
    """Installed models, preferring the XDG location over a dev checkout."""
    if MODELS.is_dir() and any(MODELS.iterdir()):
        return MODELS
    if _DEV_MODELS.is_dir():
        return _DEV_MODELS
    return MODELS


def vosk_model() -> Path | None:
    """The Vosk model directory, whichever one is present."""
    for base in (models_dir(),):
        found = sorted(base.glob("vosk-model-*"))
        if found:
            return found[0]
    return None


def piper_voices() -> Path:
    return models_dir() / "piper"


def vocab_file() -> Path:
    """User's term list if they have one, otherwise the shipped default."""
    user = CONFIG_DIR / "vocab.txt"
    return user if user.exists() else ROOT / "daemon/vocab.txt"


def config_toml() -> Path:
    """Fallback config for machines without Omarchy's shell.json."""
    user = CONFIG_DIR / "agentvoice.toml"
    return user if user.exists() else ROOT / "daemon/agentvoice.toml"


def project_dir(configured: str = "") -> Path:
    """Where the agent works.

    Empty means the home directory, which is what a bare `claude` does and so
    is the least surprising default. Anything else is expanded and resolved --
    resolved because the permission rules compare paths, and "~/src/app" and
    "/home/me/src/app/../app" have to be the same directory or a rule that
    trusts one would not trust the other.

    Falls back to home when the configured path has gone: a directory that was
    deleted or unmounted must not leave the agent running somewhere arbitrary.
    """
    home = Path.home()
    if not configured.strip():
        return home
    try:
        p = Path(configured.strip()).expanduser().resolve()
    except (OSError, RuntimeError):
        return home
    return p if p.is_dir() else home
