"""The readout that appears while it works.

Asked for as "open the agent voice window on wake, then close it a couple of
seconds later". The widget's own panel cannot do that job: it is a
KeyboardPanel, and its onOpenChanged sets focusPrimed = false and primes
WlrKeyboardFocus.Exclusive, so every open takes the keyboard -- and the flag
cannot be pre-set, because opening clears it. A reply plus its follow-up window
is around 25 seconds of somebody's typing going to a status display.

So the HUD is its own layer-shell surface with focus disabled. That property is
the whole reason it exists as a separate file, which is why it is asserted here:
nothing about it is visible to the test suite, and a later change to a
KeyboardPanel would look like a simplification.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _code(path: Path) -> str:
    """QML with its comments removed.

    Asserting against the raw text keeps matching the prose that explains the
    thing being asserted -- twice now: an `&&` in a comment about removing an
    `&&`, and "KeyboardPanel" in a comment about not being one.
    """
    out = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("//"):
            continue
        out.append(line.split("//", 1)[0] if "//" in line and "://" not in line
                   else line)
    return "\n".join(out)


HUD = _code(ROOT / "Hud.qml")


def test_the_hud_can_never_take_keyboard_focus():
    assert "WlrKeyboardFocus.None" in HUD
    assert "KeyboardPanel" not in HUD, "that is the thing this exists to avoid"
    assert "focusTarget" not in HUD and "forceActiveFocus" not in HUD


def test_it_does_not_move_anything_else_on_screen():
    """An exclusion zone would reserve space and shove windows aside every time
    the wake word fired."""
    assert "ExclusionMode.Ignore" in HUD


def test_it_shows_for_exactly_the_states_the_daemon_publishes():
    """Cross-file drift here is silent: rename a state in the daemon and the
    readout simply never appears, with nothing to explain why."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    published = set(re.findall(r'publish\("([a-z ]+)"', daemon))
    published |= set(re.findall(r'publish\(\s*"([a-z ]+)"', daemon))
    assert {"capture", "thinking", "speaking", "followup", "listening"} <= published, \
        f"the daemon's states changed: {sorted(published)}"

    busy = set(re.findall(r'vState === "([a-z]+)"', HUD))
    assert busy == {"capture", "thinking", "speaking", "followup"}, busy
    assert busy <= published, f"the HUD waits for states nothing publishes: {busy - published}"
    assert "listening" not in busy, "idle is not busy"
    assert "off" not in busy


def test_the_widget_passes_the_live_state_rather_than_re_reading_it():
    """Two watchers on one file disagree while it is being written, and the
    widget already has a FileView on it."""
    panel = _code(ROOT / "Panel.qml")
    block = panel[panel.index("  Hud {"):]
    block = block[:block.index("\n  }") + 4]
    for wired in ("vState:", "stateLabel:", "transcript:", "lingerMs:", "enabled:"):
        assert wired in block, f"{wired} not passed to the HUD"
    assert "FileView" not in HUD, "the HUD must not open the state file itself"


def test_the_transcript_is_not_shown_while_still_capturing():
    """The field holds the previous utterance until Whisper returns. Showing
    that beside "hearing you" reads as having misheard what was just said."""
    panel = _code(ROOT / "Panel.qml")
    block = panel[panel.index("  Hud {"):]
    block = block[:block.index("\n  }") + 4]
    assert 'vState === "capture" ? ""' in block


def test_both_settings_exist_in_all_three_places():
    """A setting is the manifest, the daemon's defaults, and a real control --
    the check script enforces the last two; this names the first."""
    manifest = json.loads((ROOT / "manifest.json").read_text())
    declared = {k["key"] for k in manifest["barWidget"]["schema"]}
    defaults = manifest["barWidget"]["defaults"]
    settings = (ROOT / "Settings.qml").read_text()

    for key in ("hud", "hudLingerMs"):
        assert key in declared, f"{key} missing from the manifest schema"
        assert key in defaults, f"{key} missing from the manifest defaults"
        assert f'"{key}"' in settings, f"{key} has no control"


def test_turning_it_off_closes_it_rather_than_freezing_it():
    """Otherwise disabling it mid-exchange leaves the last frame on screen with
    no timer left to take it away."""
    assert "onEnabledChanged" in HUD
    assert re.search(r"onEnabledChanged.*showing = false", HUD, re.S)


# --- the answer, in text ----------------------------------------------------

def test_the_reply_is_published_as_it_is_produced():
    """It used to be written to the state file only after the last word had been
    spoken -- which is the moment the readout starts counting down to close, so
    the answer was on screen for the linger and nothing longer."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    loop = daemon[daemon.index("for sentence in sentences("):]
    loop = loop[:loop.index("reply = \" \".join(reply_parts)")]

    assert 'last["reply"]' in loop, "the reply must be updated inside the loop"
    assert loop.count("self.state.publish") >= 2, \
        "published for both the spoken and the silent path"
    # And before speak(), which blocks until the sentence has been said.
    assert loop.index("self.state.publish") < loop.index("self.speak(sentence")


def test_a_new_turn_clears_the_previous_answer():
    """publish() merges, and the widget only updates a field it is sent, so an
    absent key left the last answer on screen through the whole next turn."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    assert '"reply": ""' in daemon, "the per-turn dict must clear it"


def test_the_readout_renders_the_reply():
    assert "hud.reply" in HUD
    panel = _code(ROOT / "Panel.qml")
    block = panel[panel.index("  Hud {"):]
    block = block[:block.index("\n  }") + 4]
    assert "reply: voice.lastReply" in block


def test_the_switch_has_its_own_section():
    """It was at the end of TALKING TO IT, which is otherwise about keys, and
    the person who asked for it could not find it there."""
    settings = _code(ROOT / "Settings.qml")
    assert 'text: "ON-SCREEN READOUT"' in settings
    header = settings.index('text: "ON-SCREEN READOUT"')
    toggle = settings.index('"hud"')
    assert header < toggle, "the header must introduce the control"


def test_no_dead_flag_is_left_behind():
    """`spoke` existed to publish "speaking" once; publishing every sentence
    made it write-only, and this project has shipped orphaned code before."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    assert "spoke = " not in daemon
