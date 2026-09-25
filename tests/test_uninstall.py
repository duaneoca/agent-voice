"""The uninstaller runs from the directory it is deleting.

`install.sh` is copied into ~/.local/share/agentvoice/app, and the widget's
Remove button runs that copy -- which then removes that same directory. That
works because bash keeps the script's fd open and the unlinked inode outlives
the `rm`, but it is worth a test rather than an argument.

What actually shipped broken was the call. The button ran

    bash -c "$APP/install.sh" --uninstall

and with `bash -c`, words after the command string become $0, $1, ... -- so
the flag never reached the script and a full *install* ran instead. That
install then did `rm -rf "$APP"` with $APP as its own source, leaving an empty
directory, a dangling `agentvoice` symlink and a service still running.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _env(home: Path, **overrides) -> dict[str, str]:
    """A fully sandboxed environment for install.sh.

    Every XDG variable the script reads has to be redirected, not just the
    ones whose absence is obvious. This passed HOME, XDG_DATA_HOME and
    XDG_CONFIG_HOME but not XDG_RUNTIME_DIR, so

        rm -rf "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agentvoice"

    fell through to its default and deleted the *developer's* live runtime
    directory. The running daemon then died on its next state publish. It
    happened three times before the journal timestamps were matched against
    when the suite had run. conftest.py sandboxes these already; building a
    fresh dict threw that away.

    test_every_xdg_variable_is_redirected keeps the list honest.
    """
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_RUNTIME_DIR": str(home / "run"),
    }
    env.update(overrides)
    return env


def test_every_xdg_variable_is_redirected(tmp_path):
    """Whatever install.sh reads, _env must override.

    A new XDG_* default in the script is a new way for these tests to reach
    out of the sandbox and touch the machine they run on.
    """
    script = (ROOT / "install.sh").read_text()
    used = set(re.findall(r"\$\{?(XDG_[A-Z_]+)", script))
    assert used, "found no XDG variables; did the pattern stop matching?"
    covered = set(_env(tmp_path))
    assert used <= covered, f"not redirected: {sorted(used - covered)}"


def _install_tree(home: Path) -> Path:
    """The layout a real install leaves behind, minus the heavy parts."""
    app = home / "data" / "agentvoice" / "app"
    app.mkdir(parents=True)
    for d in ("daemon", "bin", "desktop"):
        shutil.copytree(ROOT / d, app / d)
    for f in ("install.sh", "requirements.txt", "requirements-openwakeword.txt"):
        shutil.copy(ROOT / f, app / f)

    data = app.parent
    (data / "venv").mkdir()
    (data / "models").mkdir()
    (data / "verifiers").mkdir()
    (data / "verifiers" / "hey_jarvis.pkl").write_text("twenty-five recordings")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "agentvoice").write_text("#!/bin/sh\n")
    (home / "config" / "systemd" / "user").mkdir(parents=True)
    (home / "config" / "systemd" / "user" / "agentvoice.service").write_text("[Unit]\n")
    return app


def _run(app: Path, home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(app / "install.sh"), *args], env=_env(home),
                          capture_output=True, text=True, timeout=120)


def test_uninstall_completes_from_inside_the_directory_it_removes(tmp_path):
    app = _install_tree(tmp_path)
    data = app.parent

    done = _run(app, tmp_path, "--uninstall", "--yes")

    assert done.returncode == 0, done.stderr
    assert not app.exists()
    assert not (data / "venv").exists()
    assert not (data / "models").exists()
    assert not (tmp_path / ".local" / "bin" / "agentvoice").exists()
    assert not (tmp_path / "config" / "systemd" / "user" / "agentvoice.service").exists()


def test_uninstall_keeps_the_recordings_it_did_not_create(tmp_path):
    app = _install_tree(tmp_path)
    kept = app.parent / "verifiers" / "hey_jarvis.pkl"

    done = _run(app, tmp_path, "--uninstall", "--yes")

    assert kept.read_text() == "twenty-five recordings"
    assert str(kept.parent) in done.stdout, "kept it without saying where"


def test_uninstall_leaves_nothing_when_there_was_nothing_to_keep(tmp_path):
    """install.sh creates verifiers/ and wakewords/ itself, so testing `-d`
    asked whether it had made them, not whether the user had put anything in
    them. A first-install-then-remove promised to have kept recordings that
    were never made, and left two empty directories behind saying so."""
    app = _install_tree(tmp_path)
    data = app.parent
    (data / "verifiers" / "hey_jarvis.pkl").unlink()
    (data / "wakewords").mkdir(exist_ok=True)

    done = _run(app, tmp_path, "--uninstall", "--yes")

    assert done.returncode == 0, done.stderr
    assert not data.exists(), f"left behind: {list(data.rglob('*'))}"
    assert "verifiers" not in done.stdout


def test_declining_does_not_remove_anything_and_says_no(tmp_path):
    """Exit status matters: the button chains `&& omarchy plugin remove`."""
    app = _install_tree(tmp_path)
    done = subprocess.run([str(app / "install.sh"), "--uninstall"], env=_env(tmp_path),
                          input="n\n", capture_output=True, text=True, timeout=120)

    assert done.returncode != 0
    assert app.exists()
    assert (app.parent / "venv").exists()


@pytest.mark.parametrize("flag", ["--uninstall", "--remove"])
def test_the_flag_reaches_the_script(tmp_path, flag):
    """Guards the shape of the bug, not just its instance.

    Running the installed copy with no recognised flag is an *install*, which
    is what the widget accidentally did for weeks.
    """
    app = _install_tree(tmp_path)
    done = _run(app, tmp_path, flag, "--yes")
    assert done.returncode == 0
    assert not app.exists()


def test_uninstall_works_without_the_tools_only_the_install_needs(tmp_path):
    """Removal downloads nothing, so curl/bsdtar/jq must not gate it.

    The dependency check sat above the uninstall block, so a machine that
    never had bsdtar -- or stopped having it -- could install agentvoice and
    then not remove it. CI runners have no bsdtar, which is how this surfaced.
    """
    app = _install_tree(tmp_path)

    # The real environment with exactly three tools taken out of it, which
    # is what a CI runner looks like. Listing what to keep instead was worse:
    # the first attempt forgot dirname and failed on line 22 for the wrong
    # reason, and any such list rots the next time the script calls something.
    slim = tmp_path / "slimbin"
    slim.mkdir()
    withheld = {"curl", "bsdtar", "jq"}
    for entry in os.environ["PATH"].split(os.pathsep):
        d = Path(entry)
        if not d.is_dir():
            continue
        for tool in d.iterdir():
            if tool.name in withheld or (slim / tool.name).exists():
                continue
            try:
                (slim / tool.name).symlink_to(tool)
            except OSError:
                pass
    for tool in withheld:
        assert shutil.which(tool, path=str(slim)) is None

    done = subprocess.run(
        [str(app / "install.sh"), "--uninstall", "--yes"],
        env=_env(tmp_path, PATH=str(slim)),
        capture_output=True, text=True, timeout=120,
    )

    assert done.returncode == 0, done.stderr
    assert not app.exists()


def test_uninstall_names_a_key_left_in_a_file(tmp_path):
    """The keyring is named on the way out; the file fallback was not.

    daemon/paths.py documents ~/.config/agentvoice/endpoint.key for machines
    with no keyring, so it is exactly the kind of thing a removal should say
    it kept rather than leave for the user to find.
    """
    app = _install_tree(tmp_path)
    cfg = tmp_path / "config" / "agentvoice"
    cfg.mkdir(parents=True)
    (cfg / "endpoint.key").write_text("sk-not-a-real-key")

    done = _run(app, tmp_path, "--uninstall", "--yes")

    assert done.returncode == 0, done.stderr
    assert (cfg / "endpoint.key").read_text() == "sk-not-a-real-key"
    assert str(cfg) in done.stdout, "kept a key without saying so"


def test_uninstall_removes_the_config_dir_when_it_holds_nothing(tmp_path):
    app = _install_tree(tmp_path)
    cfg = tmp_path / "config" / "agentvoice"
    cfg.mkdir(parents=True)

    done = _run(app, tmp_path, "--uninstall", "--yes")

    assert done.returncode == 0, done.stderr
    assert not cfg.exists()


# --- the other half of the same accident -----------------------------------
# Once the flag was lost, what ran was an install, from $APP, whose very first
# act is `rm -rf "$APP"` -- deleting the tree it is about to copy from. The cp
# then failed under `set -e` and left an empty app/ behind. Reaching that line
# for real means downloading a Python and 700MB of models, so the section is
# lifted out and run against the same two shapes of input.

def _daemon_section() -> str:
    text = (ROOT / "install.sh").read_text()
    start = text.index("# --- the daemon itself")
    end = text.index("# --- commands on PATH")
    section = text[start:end]
    assert 'rm -rf "$APP"' in section, "section markers no longer bracket the copy"
    return section


def _run_section(root: Path, app: Path) -> subprocess.CompletedProcess:
    script = (
        'set -euo pipefail\n'
        'say() { :; }\n'
        'ok() { :; }\n'
        f'ROOT={root}\nAPP={app}\nDEV=0\n'
        'SELF_IS_APP=0\n'
        '[[ "$(readlink -f "$ROOT")" == "$(readlink -f "$APP")" ]] && SELF_IS_APP=1\n'
        + _daemon_section()
    )
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)


def test_installing_from_the_installed_copy_does_not_delete_it(tmp_path):
    app = _install_tree(tmp_path)

    done = _run_section(app, app)

    assert done.returncode == 0, done.stderr
    assert (app / "daemon").is_dir()
    assert (app / "bin").is_dir()
    assert (app / "install.sh").exists()


def test_installing_from_the_plugin_directory_still_replaces_the_copy(tmp_path):
    app = _install_tree(tmp_path)
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    for d in ("daemon", "bin", "desktop"):
        shutil.copytree(ROOT / d, plugin / d)
    for f in ("install.sh", "requirements.txt", "requirements-openwakeword.txt"):
        shutil.copy(ROOT / f, plugin / f)
    (app / "stale.txt").write_text("from the previous version")

    done = _run_section(plugin, app)

    assert done.returncode == 0, done.stderr
    assert (app / "daemon").is_dir()
    assert not (app / "stale.txt").exists(), "a real install must not keep old files"
