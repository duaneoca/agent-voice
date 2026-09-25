"""Offering the keybinds, without taking over someone's config.

The push-to-talk key, the interrupt and the mic release were printed for the
user to paste in, on the grounds that bindings.lua is theirs. That is true, and
the result was that they stayed unbound -- a key nobody has bound is a feature
nobody has. So it offers now, under three rules: ask first, never shadow a key
that is already bound, and put the file back if Hyprland rejects it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _lines(name: str, end: str = "}") -> str:
    """Lift a block out of install.sh by its opening line."""
    text = (ROOT / "install.sh").read_text().splitlines()
    start = next(i for i, l in enumerate(text) if l.startswith(name))
    stop = next(i for i in range(start + 1, len(text)) if text[i] == end)
    return "\n".join(text[start:stop + 1]) + "\n"


def _run(home: Path, keybinds: str, *, bindings: str | None = None,
         hyprctl: bool = False) -> subprocess.CompletedProcess:
    cfg = home / "config" / "hypr"
    cfg.mkdir(parents=True, exist_ok=True)
    if bindings is not None:
        (cfg / "bindings.lua").write_text(bindings)

    # Always stubbed, never the real one. Without this the tests reached the
    # live compositor: `hyprctl reload` ran against the developer's session, and
    # its configerrors output -- two newlines on a healthy system -- tripped the
    # rollback in every case that should have succeeded.
    bin_ = home / "bin"
    bin_.mkdir(exist_ok=True)
    if hyprctl:
        body = '#!/bin/sh\n[ "$1" = configerrors ] && echo "bad token"\nexit 0\n'
    else:
        body = '#!/bin/sh\n[ "$1" = configerrors ] && printf "\\n\\n"\nexit 0\n'
    (bin_ / "hyprctl").write_text(body)
    (bin_ / "hyprctl").chmod(0o755)

    script = (
        "set -uo pipefail\n"
        'have() { command -v "$1" >/dev/null 2>&1; }\n'
        'ok() { echo "OK: $*"; }\n'
        'warn() { echo "WARN: $*"; }\n'
        'ask() { echo "ASKED: $*"; return 1; }\n'
        f'KEYBINDS="{keybinds}"\n'
        + _lines("KEYBIND_LINES=", "o.bind(\"SUPER + ALT + SPACE\", \"Release or re-engage the mic\", \"agentvoice mic\")'")
        + _lines("offer_keybinds() {")
        + "offer_keybinds\n"
    )
    env = dict(os.environ)
    env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
                "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"})
    return subprocess.run(["bash", "-c", script], env=env,
                          capture_output=True, text=True, timeout=60)


OTHER = 'o.bind("SUPER + E", "Proton Mail", "omarchy-launch-webapp https://mail.proton.me")\n'


def test_keybinds_flag_adds_them(tmp_path):
    done = _run(tmp_path, "1", bindings=OTHER)
    text = (tmp_path / "config" / "hypr" / "bindings.lua").read_text()

    assert done.returncode == 0, done.stderr
    assert "agentvoice talk" in text
    assert "agentvoice interrupt" in text
    assert OTHER in text, "must append, never rewrite"
    assert "ASKED" not in done.stdout, "--keybinds means do not ask"


def test_it_keeps_a_backup(tmp_path):
    _run(tmp_path, "1", bindings=OTHER)
    backups = list((tmp_path / "config" / "hypr").glob("bindings.lua.agentvoice-backup.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == OTHER


def test_no_keybinds_flag_leaves_the_file_alone(tmp_path):
    done = _run(tmp_path, "0", bindings=OTHER)
    assert (tmp_path / "config" / "hypr" / "bindings.lua").read_text() == OTHER
    assert "o.bind(\"F8\"" in done.stdout, "still worth printing"


def test_neither_flag_asks_and_a_declined_answer_changes_nothing(tmp_path):
    """The empty default has to reach the prompt. `KEYBINDS` unset with an
    arithmetic test would have matched 0 and silently never asked."""
    done = _run(tmp_path, "", bindings=OTHER)
    assert "ASKED" in done.stdout
    assert (tmp_path / "config" / "hypr" / "bindings.lua").read_text() == OTHER


def test_a_key_already_bound_is_never_shadowed(tmp_path):
    taken = OTHER + 'o.bind("F8", "Something of mine", "my-command")\n'
    done = _run(tmp_path, "1", bindings=taken)

    assert (tmp_path / "config" / "hypr" / "bindings.lua").read_text() == taken
    assert "WARN" in done.stdout
    assert "F8" in done.stdout


def test_running_twice_does_not_duplicate_them(tmp_path):
    _run(tmp_path, "1", bindings=OTHER)
    first = (tmp_path / "config" / "hypr" / "bindings.lua").read_text()
    done = _run(tmp_path, "1")

    assert (tmp_path / "config" / "hypr" / "bindings.lua").read_text() == first
    assert "already" in done.stdout


def test_no_bindings_file_means_print_and_leave(tmp_path):
    """Not an Omarchy machine, or not one using bindings.lua. Creating the file
    would be inventing a config someone did not ask for."""
    done = _run(tmp_path, "1", bindings=None)
    assert not (tmp_path / "config" / "hypr" / "bindings.lua").exists()
    assert "o.bind" in done.stdout


def test_a_rejected_config_is_rolled_back(tmp_path):
    """Hyprland reloads on save, so a bad file is live at once -- and a broken
    bindings.lua costs the user their whole keyboard."""
    done = _run(tmp_path, "1", bindings=OTHER, hyprctl=True)

    assert (tmp_path / "config" / "hypr" / "bindings.lua").read_text() == OTHER, \
        "must restore the file Hyprland rejected"
    assert "put back" in done.stdout
