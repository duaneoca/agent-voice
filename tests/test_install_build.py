"""Which build is installed, and whether anything says so.

Written after a round of testing was spent on a daemon that had been quietly
replaced with an older one. The panel's install offer runs `install.sh` out
of the plugin directory -- a clone of the published repository -- so on a
machine whose daemon came from a newer checkout, clicking it puts the older
daemon back. Four fixes went backwards that way, including the speech gate,
so `logSpeech` was being honoured by code that did not contain it: the
setting looked broken while the running build behaved exactly as written.

Nothing recorded a version, so working that out meant diffing the installed
files against every recent commit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_installer_stamps_the_build_and_says_what_it_replaced():
    sh = (ROOT / "install.sh").read_text()
    assert '> "$APP/VERSION"' in sh, "the installed tree records no version"
    assert 'PREV_BUILD="$(head -1 "$APP/VERSION"' in sh, \
        "it has to read the old stamp before rm -rf deletes it"
    at = sh.index('PREV_BUILD="$(head -1')
    assert at < sh.index('rm -rf "$APP"', at), \
        "the previous build is read after it has been deleted"
    assert "replacing $PREV_BUILD" in sh, \
        "a replacement that is never reported is the bug this came from"


def test_the_banner_prints_the_build():
    """Into the journal, which is where the diagnosis happens. The lines
    underneath it are worth much less when nothing says what produced them."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    assert "build {build_id()}" in daemon


class TestBuildId:
    def test_a_stamp_is_used_when_it_exists(self, tmp_path):
        import paths

        (tmp_path / "VERSION").write_text("1.4.0 (abc1234)\n")
        assert paths.build_id(tmp_path) == "1.4.0 (abc1234)"

    def test_a_checkout_falls_back_to_git(self, tmp_path):
        import paths

        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        got = paths.build_id(tmp_path)
        # An empty repository has no describe output, which is the "unrecorded"
        # answer rather than a wrong one.
        assert got.startswith("checkout ") or got == "unrecorded", got

    def test_neither_is_not_a_made_up_number(self, tmp_path):
        """An invented version is worse than none: it is the first thing
        anybody checks and the last thing they would doubt."""
        import paths

        assert paths.build_id(tmp_path) == "unrecorded"

    def test_an_unreadable_stamp_is_not_fatal(self, tmp_path):
        import paths

        (tmp_path / "VERSION").mkdir()        # a directory, not a file
        assert paths.build_id(tmp_path) == "unrecorded"

    def test_a_blank_stamp_falls_through(self, tmp_path):
        import paths

        (tmp_path / "VERSION").write_text("\n\n")
        assert paths.build_id(tmp_path) == "unrecorded"
