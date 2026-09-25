"""Choosing a voice has to be able to get it.

The list offers every voice the project knows and marks the ones not on disk,
and the daemon substitutes a voice that is when the chosen one is missing --
so nothing breaks. But nothing downloads it either: only install.sh fetches
voices. Selecting one was a dead end that announced itself only in a log.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = (ROOT / "Settings.qml").read_text()
DAEMON = (ROOT / "daemon" / "wake_listen.py").read_text()


def test_a_voice_that_is_not_on_disk_can_be_downloaded():
    block = SETTINGS[SETTINGS.index('label: "Voice"'):]
    block = block[:block.index('label: "Live transcript')]
    assert "voicesOnDisk.indexOf" in block, "must know whether it is present"
    assert "install.sh" in block, "and offer to fetch it"
    assert "Download " in block


def test_the_offer_appears_only_when_it_is_missing():
    block = SETTINGS[SETTINGS.index('label: "Voice"'):]
    block = block[:block.index('label: "Live transcript')]
    visible = block[block.index("visible:"):]
    visible = visible[:visible.index("\n\n")]
    assert "< 0" in visible, "shown when the voice is absent from the list"
    assert "voicesOnDisk.length > 0" in visible, \
        "an empty probe result must not claim every voice is missing"


def test_the_installer_is_called_with_a_flag_it_accepts():
    accepted = set()
    # `--name)` for a switch, `--name=*)` for one that takes a value.
    for m in re.finditer(r"^\s*--([a-z-]+)(?:=\*)?(?:\|--?([a-z-]+))*\)",
                         (ROOT / "install.sh").read_text(), re.M):
        accepted.update(g for g in m.groups() if g)
    for call in re.findall(r"/install\.sh([^\"']*)", SETTINGS):
        for flag in re.findall(r"--([a-z-]+)=?", call):
            assert flag in accepted, f"install.sh does not accept --{flag}"


def test_the_installer_fetches_both_the_named_and_the_configured_voice():
    """Named on the command line, because choosing a voice fires the installer
    at once and `omarchy bar set` has not necessarily landed -- reading the
    setting alone would race the write. The configured one is still fetched
    too: a --voice run is also an install, and the voice in use has to be
    present when it finishes.
    """
    install = (ROOT / "install.sh").read_text()
    loop = install[install.index('for v in "$WANT_VOICE_ARG"'):]
    loop = loop[:loop.index("done") + 4]
    assert "$(configured voice)" in loop
    assert 'fetch_voice "${v%-*}" "${v##*-}"' in loop
    assert "--voice=*)" in install, "the flag has to be accepted"


def test_a_downloaded_voice_is_picked_up_without_touching_a_setting():
    """A file appearing changes no setting, so a check behind the
    config-changed gate would leave the substitution in place until something
    unrelated moved -- the same stall as the detection threshold that never
    reached the running engine."""
    refresh = DAEMON[DAEMON.index("    def refresh(self):"):]
    refresh = refresh[:refresh.index("    def reconcile_voice")]
    before_gate = refresh[:refresh.index("if not self.cfg.reload():")]
    assert "self.reconcile_voice()" in before_gate, \
        "the voice check must not sit behind the config-changed guard"


def test_reconciling_the_voice_is_cheap_when_nothing_changed():
    """It runs on every poll now, so it must not rebuild Piper each time."""
    body = DAEMON[DAEMON.index("    def reconcile_voice"):]
    body = body[:body.index("\n    def ", 10)]
    guard = body[body.index("if (self.speaker is not None"):]
    guard = guard[:guard.index("return") + 6]
    for condition in ("requested == wanted", "not self.speaker.superseded()"):
        assert condition in guard, f"missing {condition}"
