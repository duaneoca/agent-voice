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
