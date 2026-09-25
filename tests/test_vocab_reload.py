"""The vocabulary list is a file, and files change without settings changing.

Found by auditing every setting the daemon reads after the same shape of bug
appeared four times: a value read once and never again. Twenty-five of
twenty-five settings turned out to be reconciled, so the audit's real finding
was not a setting at all. `self._vocab` is read in Pipeline.__init__ and passed
to Whisper as initial_prompt on every transcription, so editing your own term
list -- or creating ~/.config/agentvoice/vocab.txt for the first time, which
changes which file is used -- did nothing until the service restarted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))


def test_the_stamp_notices_an_edit(tmp_path, monkeypatch):
    import wake_listen
    f = tmp_path / "vocab.txt"
    f.write_text("kubernetes\n")
    monkeypatch.setattr(wake_listen, "vocab_file", lambda: f)

    first = wake_listen.vocab_stamp()
    f.write_text("kubernetes\nquickshell\n")
    import os
    os.utime(f, (first[1] + 10, first[1] + 10))

    assert wake_listen.vocab_stamp() != first


def test_the_stamp_notices_a_different_file(tmp_path, monkeypatch):
    """Creating a user list for the first time changes which file is read, and
    the new one may be older than the shipped default it replaces -- so an
    mtime alone is not enough."""
    import wake_listen
    shipped = tmp_path / "shipped.txt"
    shipped.write_text("default\n")
    user = tmp_path / "user.txt"
    user.write_text("mine\n")
    import os
    os.utime(user, (1, 1))          # older than the one it replaces

    monkeypatch.setattr(wake_listen, "vocab_file", lambda: shipped)
    before = wake_listen.vocab_stamp()
    monkeypatch.setattr(wake_listen, "vocab_file", lambda: user)

    assert wake_listen.vocab_stamp() != before, \
        "a path change must count even when the new file is older"


def test_a_missing_file_does_not_raise(tmp_path, monkeypatch):
    import wake_listen
    monkeypatch.setattr(wake_listen, "vocab_file", lambda: tmp_path / "nope.txt")
    assert wake_listen.vocab_stamp()[1] == 0.0


def test_reconcile_reloads_only_when_it_changed(tmp_path, monkeypatch):
    import wake_listen
    f = tmp_path / "vocab.txt"
    f.write_text("alpha\n")
    monkeypatch.setattr(wake_listen, "vocab_file", lambda: f)

    pipe = object.__new__(wake_listen.Pipeline)
    pipe._vocab = wake_listen.load_vocab()
    pipe._vocab_stamp = wake_listen.vocab_stamp()
    assert "alpha" in pipe._vocab

    pipe.reconcile_vocab()
    assert "alpha" in pipe._vocab, "unchanged file must not disturb it"

    import os
    f.write_text("beta\n")
    os.utime(f, (pipe._vocab_stamp[1] + 10,) * 2)
    pipe.reconcile_vocab()

    assert "beta" in pipe._vocab and "alpha" not in pipe._vocab


def test_it_is_checked_outside_the_config_changed_guard():
    """Editing a file is not a setting change, so behind the guard it would
    wait for something unrelated to move -- the stall that hit the detection
    threshold, the voice, and the agent before it."""
    src = (ROOT / "daemon" / "wake_listen.py").read_text()
    refresh = src[src.index("    def refresh(self):"):]
    refresh = refresh[:refresh.index("\n    def ", 10)]
    before_guard = refresh[:refresh.index("if not self.cfg.reload():")]
    assert "reconcile_vocab()" in before_guard


def test_nothing_is_rebuilt_for_a_word_list():
    """The prompt is passed per transcription, so reloading it costs a read."""
    src = (ROOT / "daemon" / "wake_listen.py").read_text()
    body = src[src.index("    def reconcile_vocab"):]
    body = body[:body.index("\n    def ", 10)]
    assert "_load_whisper" not in body
    assert "initial_prompt=self._vocab" in src, "still read per transcription"
