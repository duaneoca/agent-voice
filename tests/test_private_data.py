"""Recordings of somebody's voice, and the model fitted to it.

Reported against 72df974 by a marketplace reviewer. `install.sh` pre-created
the verifier directory with a plain `mkdir -p`, which takes the user's umask;
`verifier.py` then asked for `mode=0o700` on a directory that already existed,
where mkdir ignores it; and openWakeWord writes the personal speaker model
with a plain `open(..., "wb")`. Under the default umask of 022 that is a 0755
tree of 0644 files -- readable by every local account on a machine whose
$HOME is not itself 0700, which is a default rather than a guarantee.

The same mistake was already fixed twice in this project, in `runtime.py` and
`permission_hook.py`, each with a comment saying mkdir's mode is ignored when
the directory exists. `verifier.py` had the comment describing the risk and
not the chmod that closes it. There is one helper now.
"""
from __future__ import annotations

import pathlib
import sys
from pathlib import Path

import pytest

import paths

ROOT = Path(__file__).resolve().parent.parent


class TestPrivateDir:
    def test_an_existing_loose_directory_is_tightened(self, tmp_path):
        """The whole finding in one assertion: mkdir's mode does nothing to a
        directory that is already there."""
        d = tmp_path / "verifiers"
        d.mkdir()
        d.chmod(0o755)

        paths.private_dir(d)

        assert d.stat().st_mode & 0o777 == 0o700

    def test_a_new_directory_is_created_tight(self, tmp_path):
        d = paths.private_dir(tmp_path / "a" / "b")
        assert d.stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "a").is_dir()

    def test_it_returns_the_path_so_it_can_wrap_a_call(self, tmp_path):
        assert paths.private_dir(tmp_path / "x") == tmp_path / "x"


class TestPrivateFile:
    def test_a_file_a_library_wrote_is_tightened(self, tmp_path):
        """openWakeWord writes the verifier itself, so the mode has to be
        fixed afterwards rather than requested up front."""
        f = tmp_path / "hey_claude.joblib"
        f.write_bytes(b"model")
        f.chmod(0o644)

        paths.private_file(f)

        assert f.stat().st_mode & 0o777 == 0o600

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """It runs after a library call that may not have produced anything,
        and a failure to tighten must not break the thing that succeeded."""
        paths.private_file(tmp_path / "never-written.joblib")


class TestRepairingWhatIsAlreadyOnDisk:
    """The half a forward-only fix cannot reach. Somebody who trained a
    verifier under a 0755 home still has those files readable, and writing
    new ones correctly does nothing for them."""

    def _loose_tree(self, root):
        train = root / "verifier-training" / "hey_claude" / "positives"
        train.mkdir(parents=True)
        verif = root / "verifiers"
        verif.mkdir(parents=True)
        (verif / "hey_claude.joblib").write_bytes(b"model")
        (train / "pos-001.wav").write_bytes(b"RIFF")
        for p, mode in ((verif, 0o755), (train, 0o755),
                        (verif / "hey_claude.joblib", 0o644),
                        (train / "pos-001.wav", 0o644)):
            p.chmod(mode)
        return verif, train

    def test_existing_recordings_and_models_are_repaired(self, tmp_path,
                                                         monkeypatch):
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        verif, train = self._loose_tree(tmp_path)

        fixed = paths.harden_private_trees()

        assert fixed, "nothing was repaired"
        assert verif.stat().st_mode & 0o777 == 0o700
        assert train.stat().st_mode & 0o777 == 0o700
        assert (verif / "hey_claude.joblib").stat().st_mode & 0o777 == 0o600
        assert (train / "pos-001.wav").stat().st_mode & 0o777 == 0o600

    def test_it_is_idempotent(self, tmp_path, monkeypatch):
        """It runs on every daemon start, so a second pass must find nothing
        and the startup line must not cry wolf."""
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        self._loose_tree(tmp_path)

        assert paths.harden_private_trees()
        assert paths.harden_private_trees() == []

    def test_it_leaves_the_venv_and_models_alone(self, tmp_path, monkeypatch):
        """The trees are listed rather than discovered: walking everything
        under the data directory would chmod a 600MB venv and the shared model
        downloads, which are not private and would take real time."""
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        self._loose_tree(tmp_path)
        venv = tmp_path / "venv" / "bin"
        venv.mkdir(parents=True)
        (venv / "python").write_bytes(b"#!/bin/sh")
        (venv / "python").chmod(0o755)

        paths.harden_private_trees()

        assert (venv / "python").stat().st_mode & 0o777 == 0o755
        assert "venv" not in paths.PRIVATE_TREES

    def test_a_missing_tree_is_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(paths, "DATA_DIR", tmp_path)
        assert paths.harden_private_trees() == []


def test_the_daemon_repairs_on_every_start():
    """So an upgrade fixes what an older version left behind, rather than
    waiting for the user to retrain."""
    src = (ROOT / "daemon" / "wake_listen.py").read_text()
    assert "harden_private_trees()" in src
    assert "tightened" in src, "a silent repair cannot be told from no repair"


def test_the_installer_does_not_create_them_loosely():
    """Pre-creating these with a plain mkdir is what defeated the mode= on the
    Python side in the first place."""
    sh = (ROOT / "install.sh").read_text()
    at = sh.index('mkdir -p "$DATA" "$MODELS"')
    assert 'chmod 700 "$DATA/verifiers" "$DATA/wakewords"' in sh[at:at + 600]


class TestTheTrainedModelIsTightened:
    """Driven, because the structural version was not enough: asserting that
    `private_file(` appears in verifier.py matched the call in `record_clip`
    and passed with the one after training removed. A mutation caught that."""

    def test_the_joblib_is_0600_even_though_a_library_wrote_it(
            self, tmp_path, monkeypatch):
        import types

        import verifier

        pos = tmp_path / "positives"
        neg = tmp_path / "negatives"
        for d, n in ((pos, 12), (neg, 6)):
            d.mkdir()
            for i in range(n):
                (d / f"c{i:03d}.wav").write_bytes(b"RIFF")

        out = tmp_path / "out" / "hey_test.joblib"
        monkeypatch.setattr(verifier, "resolve_model",
                            lambda name: (tmp_path / "m.onnx", "hey_test"))

        # Stand in for openWakeWord, writing the way it does: a plain open,
        # which lands at 0644 under the default umask.
        def fake_train(**kw):
            path = pathlib.Path(kw["output_path"])
            path.write_bytes(b"model")
            path.chmod(0o644)

        fake = types.ModuleType("openwakeword")
        fake.train_custom_verifier = fake_train
        monkeypatch.setitem(sys.modules, "openwakeword", fake)

        verifier.train("hey_test", pos, neg, output=out)

        assert out.exists(), "the stand-in did not write anything"
        assert out.stat().st_mode & 0o777 == 0o600, \
            "the model fitted to somebody's voice is readable by other accounts"
        assert out.parent.stat().st_mode & 0o777 == 0o700


def test_verifier_uses_the_shared_helper():
    """Three places needed mkdir-then-chmod and each hand-rolled it; two got
    it right and this one did not. A test so a fourth does not repeat it."""
    src = (ROOT / "daemon" / "verifier.py").read_text()
    assert "private_dir(" in src
    assert "private_file(" in src
    assert "mkdir(parents=True, exist_ok=True, mode=0o700)" not in src, \
        "a hand-rolled mkdir is back, without the chmod that makes it work"
