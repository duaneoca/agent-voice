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
    assert {"capture", "transcribing", "thinking", "speaking", "followup",
            "listening"} <= published, \
        f"the daemon's states changed: {sorted(published)}"

    busy = set(re.findall(r'vState === "([a-z]+)"', HUD))
    assert busy == {"capture", "transcribing", "thinking", "speaking",
                    "followup"}, busy
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


def test_the_wake_word_clears_the_previous_exchange():
    """Saying the wake word showed the last question and the last answer while
    it listened for the next one -- the right shape in the wrong tense.

    Fixed in the daemon, not the widget: the capture state carried whatever the
    previous turn left in it, and blanking one field in QML left the other.
    """
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    assert "def _empty_turn()" in daemon
    empty = daemon[daemon.index("def _empty_turn()"):]
    empty = empty[:empty.index("\n\n\n")]
    for field in ('"transcript": ""', '"reply": ""', '"ms": 0', '"audio_s": 0.0'):
        assert field in empty, f"{field} must be cleared, not omitted"

    # Every point a turn *begins* must reset first. A capture publish that
    # passes `**last` alone is a turn start; one that overrides a field is a
    # mid-turn update -- the live partial does that, and inherits the cleared
    # dict rather than rebuilding one.
    starts = daemon.split('self.state.publish("capture", **last)')[:-1]
    assert starts, "no turn-starting capture publish found"
    for chunk in starts:
        tail = chunk[-260:]
        assert "_empty_turn()" in tail, \
            f"a capture publish carries the previous turn:\n{tail}"

    assert '**{**last, "transcript": partial}' in daemon, \
        "the live partial must inherit the cleared turn, not a fresh dict"


def test_the_widget_does_not_second_guess_the_state_file():
    """With the daemon clearing it, a QML guard would be a second version of
    the same rule -- and the one that existed only covered the transcript."""
    panel = _code(ROOT / "Panel.qml")
    block = panel[panel.index("  Hud {"):]
    block = block[:block.index("\n  }") + 4]
    assert 'vState === "capture" ? ""' not in block
    assert "transcript: voice.lastTranscript" in block


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


def test_the_switch_is_in_the_group_the_page_opens_with():
    """It began at the end of TALKING TO IT, a section otherwise about keys,
    where the person who asked for it could not find it. Then its own section.
    Then, asked for again, first on the page -- which is where a switch people
    reach for belongs, above anything with a section header.
    """
    settings = _code(ROOT / "Settings.qml")
    toggle = settings.index('label: "Show what it heard and what it answered"')
    # It sits inside the first group, so it must come before every *later*
    # group's title -- and before the second title on the page, which is the
    # first one it is not itself inside.
    titles = [m.start() for m in re.finditer(r'\n\s+title: "[A-Z]', settings)]
    assert len(titles) > 1, "the page should be grouped"
    assert toggle < titles[1], "the switch has fallen below another group"

    # The duration stays below, and its header and separator go when the
    # readout is off, or there are two rules with nothing between them.
    # The switch and the one number it governs are a single setting described
    # twice: how long it stays means nothing when it is not shown. They live in
    # one box, the first on the page.
    readout = settings.index('title: "ON-SCREEN READOUT"')
    box = settings[readout:settings.index("SettingsGroup {", readout)]
    assert "hudLingerMs" in box, "the duration belongs with its switch"
    assert readout < toggle < settings.index("hudLingerMs"), \
        "the switch should open the box its duration sits in"

    # And the knob hides rather than the box, or turning the readout off would
    # take away the only way to turn it back on.
    knob = box[box.index("KnobRow {"):]
    assert 'visible: root.setting("hud", true) === true' in knob
    assert 'visible: root.setting("hud", true) === true' not in box[:box.index("KnobRow {")]


def test_no_dead_flag_is_left_behind():
    """`spoke` existed to publish "speaking" once; publishing every sentence
    made it write-only, and this project has shipped orphaned code before."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    assert "spoke = " not in daemon


def test_the_settings_page_scrolls_like_every_other_omarchy_panel():
    """Reported as the pane scrolling slower than the rest of the desktop.

    Nine scrolling panels in Omarchy attach a ScrollBar to their Flickable;
    this one did not. A bare Flickable handles the wheel itself with a short,
    momentum-less step, and with a ScrollBar attached the Controls wheel
    handling applies instead -- which is what those panels feel like.
    """
    settings = _code(ROOT / "Settings.qml")
    flick = settings[settings.index("Flickable {"):]
    flick = flick[:flick.index("Column {")]
    assert "ScrollBar.vertical" in flick, "no scrollbar, so no Controls wheel step"
    assert "flickableDirection: Flickable.VerticalFlick" in flick
    assert "interactive: contentHeight > height" in flick


def test_controls_is_imported_under_a_namespace():
    """A plain `import QtQuick.Controls` shadows Omarchy's Button, Toggle and
    Dropdown with the Controls types of the same names. qs.Ui is imported last
    so it wins at runtime, but nothing in the file says so -- and qmllint reads
    it the other way, reporting twelve properties as missing."""
    raw = (ROOT / "Settings.qml").read_text()
    assert "import QtQuick.Controls as QQC" in raw
    assert "\nimport QtQuick.Controls\n" not in raw
