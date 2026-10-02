"""spin() has to be able to run the things it is given.

The trainer wraps slow steps in `gum spin` through a helper that ran its
command as `bash -c '... exec "$@"'`. exec replaces the process with an
executable, so it cannot run a shell function: it looks for a binary of that
name and exits 127 with "exec: NAME: not found". Every caller was an external
command until the voice fetch passed a function, and then every fetch failed
while reporting only "Could not fetch <voice>; carrying on with what is here"
-- the reason captured into a temp file and deleted unread.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAINER = (ROOT / "bin" / "agentvoice-train-verifier").read_text()


def _block(name: str, end: str = "}") -> str:
    lines = TRAINER.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(name))
    stop = next(i for i in range(start + 1, len(lines)) if lines[i] == end)
    return "\n".join(lines[start:stop + 1]) + "\n"


def _code(text: str) -> str:
    """Without comments. Three assertions in this suite have now matched the
    prose explaining the very thing they forbid -- an `&&` in a comment about
    removing an `&&`, "KeyboardPanel" in a comment about not being one, and
    `exec "$@"` in a comment about why exec cannot be used."""
    return "\n".join(l for l in text.splitlines()
                      if not l.lstrip().startswith("#"))


def test_spin_can_run_a_shell_function(tmp_path):
    """The behaviour, not the spelling: a function through spin must run.

    gum is stubbed rather than required. The first version of this needed the
    real one and a /dev/tty, so it passed on a desktop and failed on CI -- and
    what is under test is spin's inner `bash -c`, not gum's spinner.
    """
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    # bash, not sh. The real gum is a Go binary and passes the environment
    # through verbatim; /bin/sh is dash on a Debian runner, and dash drops
    # environment variables whose names are not valid identifiers -- which is
    # how exported bash functions are stored (BASH_FUNC_name%%). With sh the
    # stub silently lost `mine` and the test failed with 127 on CI only, since
    # /bin/sh here is bash.
    (bin_ / "gum").write_text(
        '#!/bin/bash\n'
        '# Drop gum\'s own flags, then run whatever followed `--`.\n'
        'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
        'shift\n'
        'exec "$@"\n')
    (bin_ / "gum").chmod(0o755)

    script = (
        "set -uo pipefail\n"
        + _block("tty_flush()") + _block("echo_off()") + _block("echo_restore()")
        + _block("spin()")
        + 'have() { command -v "$1" >/dev/null 2>&1; }\n'
        'mine() { echo "ran with $1"; }\n'
        "export -f mine\n"
        f'spin "t" {tmp_path}/out -- mine hello\n'
        'rc=$?\n'
        f'echo "rc=$rc"; cat {tmp_path}/out\n'
    )
    env = {**os.environ, "TERM": "dumb",
           "PATH": f"{bin_}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(["bash", "-c", script], capture_output=True,
                          text=True, timeout=60, env=env)
    assert "rc=0" in done.stdout, done.stdout + done.stderr
    assert "ran with hello" in done.stdout


def test_spin_does_not_exec():
    """Stated separately, because the failure is invisible from the outside:
    exec's 127 looks exactly like a command that ran and failed."""
    spin = _code(_block("spin()"))
    assert 'exec "$@"' not in spin, "exec cannot run a function or a builtin"
    assert '"$@" >"$o"' in spin


def test_the_voice_fetcher_exists_inside_spin():
    """Necessary as well as dropping exec: spin's inner shell is a new process,
    and an unexported function is not in it."""
    assert "export -f fetch_speaker" in TRAINER
    assert "export PIPER_BASE" in TRAINER, "the function reads it"


def test_a_failed_fetch_says_why():
    """The stderr was collected into a temp file and removed unread, which is
    how a wrapper written to capture errors managed to hide one for good."""
    fetch = TRAINER[TRAINER.index("Could not fetch"):]
    fetch = fetch[:fetch.index("rm -f")]
    assert "SPIN_ERR" in fetch, "the captured reason must be shown"


class TestRecordFreshKeepsTheOldClipsUntilItHasNew:
    """"Record fresh" deleted twenty-five recordings before making one.

    `rm -f "$POS"/*.wav` ran the moment the question was answered, so a
    cancel halfway through recording left the user with neither set -- and
    the cancel path then said "Nothing was saved", which was the opposite of
    what had just happened.
    """

    def _harness(self, pos, old_marker="", action="restore"):
        script = (
            "set -uo pipefail\n"
            f"POS={pos}\n"
            + _block("restore_old_positives() {")
            + _block("discard_old_positives() {")
            + f'OLD_POS="$POS.previous.$$"\n'
            + 'mkdir -p "$OLD_POS"\n'
            + 'mv "$POS"/*.wav "$OLD_POS"/ 2>/dev/null || true\n'
            + f"{action}_old_positives\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=30)

    def test_a_cancel_puts_them_back(self, tmp_path):
        pos = tmp_path / "positives"
        pos.mkdir()
        for i in range(3):
            (pos / f"{i}.wav").write_text("clip")

        done = self._harness(pos, action="restore")

        assert done.returncode == 0, done.stderr
        assert sorted(p.name for p in pos.glob("*.wav")) == \
            ["0.wav", "1.wav", "2.wav"]
        assert not list(tmp_path.glob("*.previous.*")), "left a stray directory"

    def test_a_finished_run_discards_them(self, tmp_path):
        pos = tmp_path / "positives"
        pos.mkdir()
        (pos / "0.wav").write_text("clip")

        done = self._harness(pos, action="discard")

        assert done.returncode == 0, done.stderr
        assert not list(pos.glob("*.wav"))
        assert not list(tmp_path.glob("*.previous.*"))

    def test_nothing_is_deleted_before_the_new_clips_exist(self):
        """The guard this closes, read off the source: the only `rm` of the
        positives must be the one that runs after training."""
        body = _code(TRAINER)
        assert 'rm -f "$POS"/*.wav' not in body, \
            "clips must be moved aside, not deleted, before recording"
        train_at = body.index('"$VERIFIER" train ')
        discard_at = body.index("discard_old_positives\n", train_at)
        assert discard_at > train_at

    def test_the_cancel_message_does_not_claim_a_loss_that_did_not_happen(self):
        block = _block("cancelled() {")
        assert "Nothing was saved" not in block
        assert "restore_old_positives" in block
