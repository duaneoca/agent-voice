"""The trainer has to pick an interpreter that can actually fit a model.

Everything heavy in `verifier.py` is imported inside the function that needs
it, which is the right shape -- the check script enforces it -- but it moves
the moment of failure. `find_python` chose the first interpreter that *existed*,
so an environment without scikit-learn was selected happily and raised
ImportError at the first line of the fit: after twenty-five recordings and
about two minutes of somebody saying their wake word into a microphone, with
nothing kept to show for it.

Three outcomes, and the difference between the last two is the whole point:
"no environment" and "that environment cannot train" have different fixes, so
they cannot share a message.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAINER = (ROOT / "bin" / "agentvoice-train-verifier").read_text()


def _functions() -> str:
    """can_train and find_python, lifted out of the trainer.

    Running them rather than reading them: what is asserted is an exit status
    against a real interpreter, and the rest of the script opens a gum flow
    that wants a terminal and a microphone.
    """
    lines = TRAINER.splitlines()
    out = []
    for name in ("can_train()", "find_python()"):
        start = next(i for i, l in enumerate(lines) if l.startswith(name))
        stop = next(i for i in range(start + 1, len(lines)) if lines[i] == "}")
        out.append("\n".join(lines[start:stop + 1]))
    return "\n".join(out) + "\n"


def _find(state: str, repo: str, explicit: str = "") -> tuple[int, str]:
    script = _functions() + 'out="$(find_python)"; rc=$?; echo "$rc ${out:-}"\n'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()),
             "STATE": state, "REPO": repo, "AGENTVOICE_PYTHON": explicit,
             "CLIPS": "25"})
    rc, _, path = proc.stdout.strip().partition(" ")
    return int(rc), path


def test_an_interpreter_that_cannot_train_is_refused_before_the_first_clip():
    """/usr/bin/python3 is the realistic version of this: present, runnable,
    and without scikit-learn. Named explicitly, so it is the only candidate."""
    rc, path = _find("/nonexistent", "/nonexistent", "/usr/bin/python3")
    assert rc == 2, f"a python with no sklearn was accepted: {path}"
    assert path == "/usr/bin/python3", \
        "the one that cannot train must still be named, to say which it was"


def test_nothing_at_all_is_a_different_answer():
    rc, _ = _find("/nonexistent", "/nonexistent")
    assert rc == 1, "no interpreter must not report the same thing as a poor one"


def _stub(path: Path, can_train: bool) -> Path:
    """An executable standing in for an interpreter.

    `can_train` only asks the candidate to run a `-c` snippet and looks at the
    status, so a two-line script is a faithful stand-in -- and it keeps this
    test off the question of what happens to be installed. The first version
    used the checkout's own bench/.venv and skipped when there was none, which
    is the case on every CI runner: it turned the one job that forbids skips
    red, having proved nothing on any machine without a dev venv.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit %d\n" % (0 if can_train else 1))
    path.chmod(0o755)
    return path


def test_capability_wins_over_position(tmp_path):
    """The order is install first, then checkout -- but a reinstall that
    dropped a dependency would otherwise pin the trainer to the environment
    that cannot run it while a working one sat next door."""
    state, repo = tmp_path / "state", tmp_path / "repo"
    _stub(state / "venv" / "bin" / "python", can_train=False)
    good = _stub(repo / "bench" / ".venv" / "bin" / "python", can_train=True)

    rc, path = _find(str(state), str(repo))
    assert rc == 0, "the candidate that can train was passed over"
    assert path == str(good)


def test_the_installed_environment_is_still_preferred_when_it_works(tmp_path):
    """Capability is a tie-breaker, not a reordering: when both can train, the
    daemon's own environment is the one the trainer should fit against."""
    state, repo = tmp_path / "state", tmp_path / "repo"
    installed = _stub(state / "venv" / "bin" / "python", can_train=True)
    _stub(repo / "bench" / ".venv" / "bin" / "python", can_train=True)

    rc, path = _find(str(state), str(repo))
    assert rc == 0
    assert path == str(installed)


def test_the_two_failures_do_not_share_a_message():
    """One says run the installer; the other says that running it again is
    exactly the fix, and names the missing packages."""
    block = TRAINER[TRAINER.index('PY="$(find_python)"'):]
    block = block[:block.index("\nVERIFIER=")]
    assert "found == 1" in block and "found == 2" in block, \
        "both outcomes must be handled, or exit 2 reads as 'no environment'"
    assert "scikit-learn" in block, "the missing dependency should be named"
    assert "exit 1" in block


def test_the_capability_check_does_not_import_the_heavy_module():
    """Importing openWakeWord costs seconds and loads models, and the question
    is only whether it is installed. This runs before the opening screen."""
    block = _functions()
    assert "find_spec" in block
    assert "import openwakeword" not in block
