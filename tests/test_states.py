"""Every state the daemon publishes has to mean something on screen.

Watching it work, the status went from speaking-to-it straight to
"TRANSCRIBING" and then the answer appeared -- because transcription and the
agent's turn were one state called `thinking`, labelled "TRANSCRIBING". The
label described the shorter half and then sat there through the longer one,
which is the part anyone actually waits through.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DAEMON = (ROOT / "daemon" / "wake_listen.py").read_text()
PANEL = (ROOT / "Panel.qml").read_text()


def published_states() -> set[str]:
    return set(re.findall(r'publish\(\s*"([a-z ]+)"', DAEMON))


def test_the_phases_a_turn_passes_through_are_all_distinct():
    states = published_states()
    for phase in ("capture", "transcribing", "thinking", "speaking",
                  "followup", "listening"):
        assert phase in states, f"{phase} is not published"


def test_transcribing_is_published_before_transcription_and_thinking_after():
    """They were the same state, so the two phases could not be told apart."""
    body = DAEMON[DAEMON.index('publish("transcribing"'):]
    assert "self.pipe.transcribe(buf)" in body[:400], \
        "transcribing must be published immediately before transcribing"

    answer = DAEMON[DAEMON.index("    def answer(self"):]
    answer = answer[:answer.index("\n    def ", 10)]
    assert 'publish("thinking"' in answer, \
        "thinking belongs to the agent's turn, not to Whisper's"


def test_every_published_state_has_a_label():
    """A state with no branch falls through to "STARTING", which is what the
    user would read while it worked."""
    labels = PANEL[PANEL.index("readonly property string stateLabel"):]
    labels = labels[:labels.index("\n  }")]
    for state in published_states() - {"off"}:
        assert f'vState === "{state}"' in labels, f"{state} has no label"


def test_every_published_state_has_an_icon():
    icons = PANEL[PANEL.index("readonly property string icon"):]
    icons = icons[:icons.index("\n  }")]
    for state in published_states() - {"off", "listening"}:
        assert f'vState === "{state}"' in icons, f"{state} has no icon"
    assert 'vState === "listening"' in icons


def test_transcribing_and_thinking_do_not_share_a_label():
    labels = PANEL[PANEL.index("readonly property string stateLabel"):]
    labels = labels[:labels.index("\n  }")]
    found = dict(re.findall(r'vState === "([a-z]+)"\) return "([^"]*)"', labels))
    assert found.get("transcribing") != found.get("thinking"), found
    assert found.get("thinking") == "THINKING", found


def test_stopping_during_transcription_discards_the_turn():
    """The interrupt guard admitted `thinking`, which covered transcription, and
    answer() clears the flag on entry -- so a stop pressed while Whisper ran was
    accepted and then thrown away, and the reply arrived anyway."""
    guard = DAEMON[DAEMON.index("def _on_interrupt"):]
    guard = guard[:guard.index("\n    def ", 10)]
    assert '"transcribing"' in guard, "stop must be allowed while transcribing"

    loop = DAEMON[DAEMON.index('publish("transcribing"'):]
    loop = loop[:loop.index("if follow_up and self.enabled")]
    checked = loop.index("self._interrupt.is_set()")
    answered = loop.index("self.answer(text, last)")
    assert checked < answered, "the flag must be read before the agent is called"
    assert "self._interrupt.clear()" in loop[checked:answered], \
        "and cleared, or the next turn starts already interrupted"


# --- the verifier as a switch, not a file ----------------------------------
# Asked: with two people using this computer, how do you turn the verifier off?
# You could not. `use_verifier` had existed in OwwWake since it was written and
# nothing in the daemon passed it -- only `agentvoice monitor --no-verifier`.
# The only way to stop verifying was to delete the .joblib, which also threw
# away the twenty-five recordings behind it.

SETTINGS = (ROOT / "Settings.qml").read_text()


def test_the_daemon_honours_the_setting():
    build = DAEMON[DAEMON.index("def _build_wake"):]
    build = build[:build.index("\n    def ", 10)]
    assert 'use_verifier=cfg.bool("useVerifier")' in build


def test_switching_it_off_rebuilds_the_engine():
    """openWakeWord takes the verifier as a constructor argument, so unlike the
    threshold this cannot be assigned to a running engine."""
    spec = DAEMON[DAEMON.index("want = (engine,"):]
    spec = spec[:spec.index("self._build_wake(cfg, engine)")]
    assert 'cfg.bool("useVerifier")' in spec


def test_the_three_states_are_distinguishable_on_screen():
    """In use, trained-but-off, and never trained. Reading the middle one as
    the last would send someone back to redo work they had already done."""
    # By content, not by title: this box has been called YOUR VOICE and then
    # TRAIN WAKE WORD TO YOUR VOICE, and will be called something else again.
    start = SETTINGS.index("The stock wake models are speaker independent")
    box = SETTINGS[start:SETTINGS.index("SettingsGroup {", start)]
    assert "In use for" in box
    assert "switched " in box and "The training is kept." in box
    assert "No verifier for" in box
    assert 'label: "Only wake for my voice"' in box


def test_the_banner_does_not_call_a_disabled_verifier_missing():
    build = DAEMON[DAEMON.index("def _build_wake"):]
    build = build[:build.index("\n    def ", 10)]
    assert "no verifier yet" in build
    assert "your verifier is off" in build, \
        "a trained verifier switched off must not read as absent"


def test_the_setting_exists_in_all_three_places():
    import json
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert "useVerifier" in {k["key"] for k in manifest["barWidget"]["schema"]}
    assert manifest["barWidget"]["defaults"]["useVerifier"] is True
    assert '"useVerifier": True' in (ROOT / "daemon" / "runtime.py").read_text()
    assert "useVerifier" in SETTINGS


def test_it_defaults_to_on():
    """Off by default would mean a verifier someone trained does nothing until
    they find the switch."""
    import json
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["barWidget"]["defaults"]["useVerifier"] is True
    assert 'root.setting("useVerifier", true)' in SETTINGS, \
        "the QML fallback must agree with the manifest"


# --- the live transcript, which went nowhere anyone could see ---------------

def test_the_partial_is_published_not_only_printed():
    """"Live transcript while you speak" costs 114MB of resident Vosk and went
    to stdout alone. Under systemd that is the journal, so the one setting
    whose entire purpose is to be watched was invisible to anyone not tailing
    it -- reported as the feature simply not working."""
    loop = DAEMON[DAEMON.index("if partial and partial != shown_partial:"):]
    loop = loop[:loop.index("\n                now = time.time()")]
    assert "self.state.publish" in loop, "printing it is not showing it"
    assert '"transcript": partial' in loop, \
        "sent as the transcript, so Whisper's version replaces it in place"


def test_the_partial_is_published_only_when_it_changes():
    """Vosk emits one per frame, most of them identical. Publishing every one
    would rewrite the state file about twelve times a second for no change."""
    loop = DAEMON[DAEMON.index("if partial and partial != shown_partial:"):]
    loop = loop[:loop.index("self.state.publish")]
    assert "shown_partial = partial" in loop


def test_the_guard_resets_where_a_turn_begins():
    """Left over from the previous turn it would suppress the first partial of
    the next one, which is exactly the word you are watching for."""
    starts = DAEMON.count("last = _empty_turn()")
    resets = DAEMON.count('shown_partial = ""')
    assert resets >= starts, f"{starts} turn starts, {resets} resets"
