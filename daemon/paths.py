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
import shutil
import subprocess
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
    Where the agent *runs* and where it may write without asking are two
    different questions, though -- see `scope_root`.
    """
    home = Path.home()
    if not configured.strip():
        return home
    try:
        p = Path(configured.strip()).expanduser().resolve()
    except (OSError, RuntimeError):
        return home
    return p if p.is_dir() else home


def scope_root(configured: str = "") -> Path | None:
    """The one directory "edits" may write in without asking, or None.

    Deliberately not `project_dir`. That answers "where should the agent
    run", and its answer for an unset or vanished setting is the home
    directory -- the right place to start a shell, and the wrong thing to
    hand a blanket write permission to. Home holds ~/.bashrc, the systemd
    user units, ~/.ssh and shell.json -- and shell.json holds the permission
    level itself, so an agent that could write it unasked could promote
    itself to "trusted".

    So: no configured project, or one that has gone, means no silent writes,
    and "edits" behaves as "ask" until a real directory is chosen. A typo
    narrows the permission rather than widening it to all of home.
    """
    if not configured.strip():
        return None
    try:
        p = Path(configured.strip()).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    if not p.is_dir():
        return None
    # An "edits" scope of / or of home itself is the same blanket grant by
    # another route, so it is refused the same way.
    home = Path.home()
    if p == Path(p.root) or p == home or p in home.parents:
        return None
    return p


def _keyring(**attrs) -> str:
    """Read a secret from the freedesktop secret service, or "".

    Used ahead of the file because the keyring is where a desktop key
    belongs: it is unlocked at login by the same session that runs this
    daemon (gnome-keyring lives under user@.service, as we do), so a
    background service can read it without a prompt it has no way to answer.
    That is the thing 1Password's CLI cannot do here -- its desktop
    integration wants an interactive unlock, and a systemd unit has no
    terminal to unlock in.
    """
    tool = shutil.which("secret-tool")
    if not tool:
        return ""
    argv = [tool, "lookup"]
    for key, value in attrs.items():
        argv += [key, value]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _keyring_has_hosts() -> bool:
    """True when at least one key is stored against a specific endpoint host.

    Distinguishes "one endpoint, one key" -- where a generic key or the file
    is exactly right -- from "several endpoints, keyed by host", where a miss
    has to mean no key rather than somebody else's.
    """
    tool = shutil.which("secret-tool")
    if not tool:
        return False
    try:
        # --all, because without it `search` stops at the first match -- and
        # the first match may well be the generic entry, which would make a
        # keyring full of per-host keys look as though it had none.
        out = subprocess.run([tool, "search", "--all", "service", "agentvoice"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return "attribute.endpoint" in (out.stdout + out.stderr)


def endpoint_key(host: str = "") -> str:
    """The API key for an OpenAI-compatible endpoint, or "".

    Deliberately not a setting. shell.json is the desktop's config file --
    world readable, copied between machines, pasted into bug reports -- and a
    key does not belong there. Omarchy has no key store of its own to borrow:
    it installs each agent CLI and lets it handle its own authentication.

    Looked for in order of how much the machine protects it:

      1. the environment, for a key that should never touch disk at all
      2. the login keyring, keyed by endpoint host, so an OpenAI key and an
         xAI key can be stored side by side rather than swapped in a file
      3. the keyring under a generic name, for a single endpoint
      4. ~/.config/agentvoice/endpoint.key, for machines with no keyring

    A local endpoint such as Ollama needs no key at all, so absence is
    normal rather than an error.
    """
    # AGENTVOICE_ENDPOINT_KEY is ours: whoever set it meant it for whatever
    # endpoint is configured. A vendor's variable is not -- it is a credential
    # for that vendor, and the docstring's own worry about handing an OpenAI
    # key to api.x.ai was happening here, as was handing it to a LAN box.
    value = os.environ.get("AGENTVOICE_ENDPOINT_KEY", "").strip()
    if value:
        return value
    for var, owner in (("OPENAI_API_KEY", "api.openai.com"),
                       ("XAI_API_KEY", "api.x.ai")):
        value = os.environ.get(var, "").strip()
        if value and host.split(":")[0].lower() == owner:
            return value

    if host:
        found = _keyring(service="agentvoice", endpoint=host)
        if found:
            return found
        # Once any key is filed under a host, the absence of one for *this*
        # host means there is no key for it -- not that some other endpoint's
        # key will do. Falling through here would hand an OpenAI credential
        # to api.x.ai on the next settings change, which is a small leak but
        # an entirely avoidable one.
        if _keyring_has_hosts():
            return ""
    found = _keyring(service="agentvoice", key="endpoint")
    if found:
        return found

    key_file = CONFIG_DIR / "endpoint.key"
    try:
        raw = key_file.read_text()
    except OSError:
        return ""
    # A key anyone else on the machine can read is not a secret. Refused
    # rather than fixed silently, because the right mode is the user's to
    # set and a 401 with an explanation beats a quiet chmod.
    try:
        mode = key_file.stat().st_mode & 0o077
    except OSError:
        mode = 0
    if mode:
        print(f"agentvoice: ignoring {key_file}: mode {mode | 0o600:o} lets "
              f"others read it. chmod 600 it.")
        return ""
    # Tolerant about shape, because the failure is otherwise a 401 that says
    # nothing: people reasonably write OPENAI_API_KEY=sk-... out of habit, or
    # leave the quotes on, or add a comment line. Take the first line that
    # looks like a value and strip the furniture off it.
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            line = line.split("=", 1)[1].strip()
        return line.strip("'\"")
    return ""
