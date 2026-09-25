"""A missing voice must not mean silence, and a lost /run must not be fatal.

Both come from one session. The settings list every voice the project knows,
marking the ones not downloaded; selecting one raised inside Speaker, left the
daemon with no speaker, and nothing retried -- the reload that would retry is
gated behind a config *change*. Speech stayed off through a disable/re-enable,
and the only cure found was switching the wake engine to Vosk and back, which
supplied the change by accident.

Separately the daemon died twice with FileNotFoundError on
/run/user/1000/agentvoice/state.tmp: the directory is created once at startup,
and this project's own uninstaller removes that path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def voices(tmp_path, monkeypatch):
    """Redirect piper_voices() at the module that reads it."""
    import runtime
    d = tmp_path / "piper"
    d.mkdir()
    monkeypatch.setattr(runtime, "piper_voices", lambda: d)
    return d


def _voice(dir_: Path, name: str) -> None:
    (dir_ / f"en_US-{name}.onnx").write_bytes(b"not a real model")


def test_a_voice_that_is_not_downloaded_falls_back_to_one_that_is(voices):
    from runtime import Speaker
    _voice(voices, "lessac-medium")

    picked, substituted = Speaker._resolve("joe-medium")

    assert picked == "lessac-medium"
    assert substituted == "joe-medium", "must report what it could not honour"


def test_the_default_voice_is_preferred_when_substituting(voices):
    from runtime import Speaker
    for name in ("amy-medium", "lessac-medium", "ryan-medium"):
        _voice(voices, name)

    picked, _ = Speaker._resolve("joe-medium")

    assert picked == "lessac-medium"


def test_substitution_is_deterministic_without_the_default(voices):
    from runtime import Speaker
    for name in ("ryan-medium", "amy-medium"):
        _voice(voices, name)

    assert Speaker._resolve("joe-medium")[0] == "amy-medium"


def test_a_voice_that_is_downloaded_is_used_unchanged(voices):
    from runtime import Speaker
    _voice(voices, "lessac-medium")
    _voice(voices, "joe-medium")

    assert Speaker._resolve("joe-medium") == ("joe-medium", "")


def test_no_voices_at_all_still_raises(voices):
    """Substituting requires something to substitute. With an empty directory
    there is nothing to say, and the caller's message is the right outcome."""
    from runtime import Speaker
    with pytest.raises(FileNotFoundError):
        Speaker._resolve("lessac-medium")


def test_a_substitution_is_superseded_once_the_voice_arrives(voices, monkeypatch):
    """The reconciliation half. install.sh can fetch the voice while the daemon
    runs, and the substitution must not outlive the download."""
    import runtime
    _voice(voices, "lessac-medium")

    spk = object.__new__(runtime.Speaker)
    spk.name = "lessac-medium"
    spk.substituted_for = "joe-medium"
    spk.requested = "joe-medium"

    assert spk.superseded() is False
    _voice(voices, "joe-medium")
    assert spk.superseded() is True


def test_a_voice_used_as_asked_is_never_superseded(voices):
    import runtime
    _voice(voices, "lessac-medium")
    spk = object.__new__(runtime.Speaker)
    spk.substituted_for = ""

    assert spk.superseded() is False


def test_publish_survives_losing_the_runtime_directory(tmp_path, monkeypatch):
    import runtime
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "agentvoice")
    state = runtime.StateFile()
    state.publish("listening")
    assert json.loads(state.path.read_text())["state"] == "listening"

    # What the uninstaller does, and what tmpfs cleanup can do.
    import shutil
    shutil.rmtree(state.path.parent)

    state.publish("off")                       # used to kill the daemon
    assert json.loads(state.path.read_text())["state"] == "off"


def test_clear_does_not_replace_the_error_it_is_unwinding(tmp_path, monkeypatch):
    """clear() runs on the way out, including while handling a failure. It
    raised the same FileNotFoundError, which hid the original traceback."""
    import runtime
    monkeypatch.setattr(runtime, "RUNTIME_DIR", tmp_path / "agentvoice")
    state = runtime.StateFile()
    state.publish("listening")

    # A parent that cannot be recreated, so publish genuinely cannot succeed.
    import shutil
    shutil.rmtree(state.path.parent)
    (tmp_path / "agentvoice").write_text("now a file, not a directory")

    state.clear()                              # must not raise
