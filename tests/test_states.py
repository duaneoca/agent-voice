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
    """What the daemon says it can publish.

    Read from the declared list rather than grepped out of publish() calls:
    the grep broke the moment one state moved into a variable, which is a
    test failing for a reason that has nothing to do with what it checks.
    """
    import wake_listen
    return set(wake_listen.STATES)


def test_the_phases_a_turn_passes_through_are_all_distinct():
    states = published_states()
    for phase in ("capture", "transcribing", "thinking", "speaking",
                  "followup", "listening"):
        assert phase in states, f"{phase} is not published"


def test_every_declared_state_is_actually_reachable():
    """The other direction: a name in the list that nothing publishes is a
    case the panel carries for a state that can never arrive."""
    import wake_listen
    published = set(re.findall(r'publish\(\s*"([a-z]+)"', DAEMON))
    # off / locked / yielded / listening are chosen inside _publish and
    # published through a variable, so they appear as bare literals there
    # rather than as an argument to publish().
    published |= set(re.findall(r'"(off|locked|yielded|listening)"', DAEMON))
    for state in wake_listen.STATES:
        assert state in published, f"{state} is declared but never published"


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
    loop = loop[:loop.index("if follow_up and self.listening")]
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
    assert 'use_verifier=cfg.get_bool("useVerifier")' in build


def test_switching_it_off_rebuilds_the_engine():
    """openWakeWord takes the verifier as a constructor argument, so unlike the
    threshold this cannot be assigned to a running engine."""
    spec = DAEMON[DAEMON.index("want = (engine,"):]
    spec = spec[:spec.index("self._build_wake(cfg, engine)")]
    assert 'cfg.get_bool("useVerifier")' in spec


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


def test_the_service_is_started_and_never_enabled():
    """A microphone daemon that starts itself at every login is a privacy
    default, not a convenience one. `agentvoice start` does exactly that -- it
    starts the unit and does not enable it -- so consent is per session. Asserted
    because `systemctl --user enable` is a one-word change that looks like a
    convenience fix, and because the README now promises this behaviour."""
    cli = (ROOT / "bin" / "agentvoice").read_text()
    assert "systemctl --user start" in cli
    assert "--user enable" not in cli, "enabling makes the mic outlive the session"
    readme = (ROOT / "README.md").read_text()
    assert "per session" in readme, "the promise has to be written down"


# --- the switch that stopped answering --------------------------------------

def _uncommented(qml: str) -> str:
    """QML with its `//` comments removed.

    Asserting against raw text keeps matching the prose that explains the
    thing being asserted -- the sibling suites have the same helper for the
    same reason, and this one caught it again immediately: a comment saying
    "Not onExited" satisfied a search for "onExited".
    """
    out = []
    for line in qml.splitlines():
        if line.strip().startswith("//"):
            continue
        out.append(line.split("//", 1)[0] if "//" in line and "://" not in line
                   else line)
    return "\n".join(out)


def test_the_switch_recovers_from_a_spawn_that_never_started():
    """`busy` swallows clicks in Omarchy's ToggleSwitch, and onExited is not
    emitted for a process that failed to start.

    Measured against Quickshell rather than assumed: a Process pointed at a
    missing binary logs "failed to start", sets `running` back to false, and
    never emits `exited`. The engine is a second, larger install, so
    `agentvoice` is genuinely absent the first time anyone opens the panel --
    one click then left `busy` true for the life of the plugin instance and
    the switch ignored every click afterwards, including after the install
    that was supposed to fix it.
    """
    panel = (ROOT / "Panel.qml").read_text()
    block = _uncommented(panel[panel.index("    id: switcher"):])
    block = block[:block.index("\n  }")]
    assert "onRunningChanged" in block, \
        "busy must be cleared on a stop, not only on an exit"
    assert "if (!running)" in block, "it fires on the way up as well"
    assert "voice.busy = false" in block
    # onExited may exist -- it is how a failed spawn is told apart from a real
    # one -- but it must not be what clears `busy`, because for the failure
    # that matters it is never emitted at all.
    exited_handler = block[block.index("onExited"):] if "onExited" in block else ""
    exited_handler = exited_handler[:exited_handler.index("\n")] if exited_handler else ""
    assert "busy" not in exited_handler, \
        "busy must not be cleared from onExited, which a failed spawn never emits"


def test_a_spawn_that_never_started_is_said_out_loud():
    """The report was "the switch won't go on and there is no indication as to
    why". Silence is the worst answer a control can give: it cannot be told
    apart from a broken install, so the user has nothing to act on."""
    panel = _uncommented((ROOT / "Panel.qml").read_text())
    block = panel[panel.index("    id: switcher"):]
    block = block[:block.index("\n  }")]
    assert "exited" in block, "a failed spawn must be distinguishable from an exit"
    assert "voice.note" in block, "and it must leave something the panel can show"

    # And the panel has to actually draw it, or it is another silent flag.
    assert "visible: voice.note !== \"\"" in panel
    assert "text: voice.note" in panel


def test_the_switch_is_hidden_until_there_is_an_engine_to_switch():
    """The install offer in the same panel says a switch that cannot turn
    anything on should not be shown. The hero is never hidden, so it was
    shown anyway -- directly above that offer."""
    panel = (ROOT / "Panel.qml").read_text()
    block = panel[panel.index("trailingControl: Component {"):]
    block = block[:block.index("\n        }")]
    assert "visible: voice.engineInstalled" in block


def test_every_way_of_toggling_is_guarded_not_just_the_switch():
    """Hiding the switch guarded one of three entry points.

    The bar icon's middle click and its right click both call toggleService
    directly, so with no engine installed they spawned `agentvoice toggle`,
    the spawn failed, and the user got nothing -- which is exactly what was
    reported, thirteen seconds after the plugin was added and two seconds
    before the engine install was started.

    So the guard belongs in toggleService, where every caller must pass.
    """
    panel = _uncommented((ROOT / "Panel.qml").read_text())
    fn = panel[panel.index("function toggleService()"):]
    fn = fn[:fn.index("\n  }")]
    assert "engineInstalled" in fn, \
        "the guard must be in the function every entry point calls"
    assert fn.index("engineInstalled") < fn.index("switcher.running"), \
        "it has to refuse before it spawns, not after"
    assert "return" in fn, "the refusal has to stop, not fall through"

    # There must still be more than one caller -- if this ever drops to one,
    # the comment above is stale and the reasoning should be re-read.
    assert panel.count("toggleService()") >= 3, \
        "fewer callers than the bug had; re-check whether the guard still belongs here"


def test_a_released_microphone_has_its_own_label():
    """`off` is published in two different situations and only one of them was
    drawn: the service not running (the !serviceActive branch says OFF), and
    the service running with the microphone released. The second fell through
    to the default and read "STARTING" -- a daemon that had been up for days
    describing itself as still starting, with nothing to say why it was deaf.

    The check script skipped `off` on the grounds the first case covered it,
    so the one state with a hole was the one state exempt from the check.
    """
    panel = _uncommented((ROOT / "Panel.qml").read_text())
    for block in ("icon", "stateLabel"):
        start = panel.index(f"readonly property string {block}:")
        body = panel[start:panel.index("\n  }", start)]
        assert 'vState === "off"' in body, f"{block} has no case for a released mic"

    label = panel[panel.index("readonly property string stateLabel:"):]
    label = label[:label.index("\n  }")]
    assert label.index('vState === "off"') < label.index('return "STARTING"'), \
        "the off case must come before the fallthrough it was lost in"


def test_muting_is_reachable_from_the_panel_and_guarded_once():
    """Reported as: "I had no idea that that's how mute worked, and I was
    turning off voice and turning it back on when needed." A right click on
    the bar icon and a keybind were the only ways in, and neither announces
    itself. The icon that reports whether the microphone is open is now the
    control that changes it.
    """
    panel = _uncommented((ROOT / "Panel.qml").read_text())
    assert "function toggleMic()" in panel

    fn = panel[panel.index("function toggleMic()"):]
    fn = fn[:fn.index("\n  }")]
    assert "engineInstalled" in fn, "the same refusal as the service toggle"
    assert "serviceActive" in fn, "nothing to mute when nothing is listening"

    # Both entry points go through it, so there is one guard rather than two.
    assert panel.count("voice.toggleMic()") >= 2, \
        "the bar icon and the panel icon should share the one path"
    assert "micToggle.running = true" not in panel[panel.index("onPressed:"):], \
        "the bar's right click must not reach the Process directly any more"


# --- what reaches the journal ----------------------------------------------

def test_a_terminal_always_shows_the_words_and_the_journal_does_not():
    """A terminal is ephemeral and somebody is looking at it -- that is the
    interface. The journal keeps entries for weeks, so logging replies means
    keeping a transcript of everything the agent read out until it rotates."""
    import wake_listen
    assert wake_listen.speech_is_logged(True) is True, "opt-in must win"

    import io
    import sys as _sys
    real = _sys.stdout
    try:
        _sys.stdout = io.StringIO()          # not a tty
        assert wake_listen.speech_is_logged(False) is False
        assert wake_listen.speech_is_logged(True) is True
    finally:
        _sys.stdout = real


def test_every_print_carrying_speech_is_gated():
    """Parsed, not grepped, and that is the whole point.

    The grep this replaces matched the first physical line of each `print(`
    and looked for content on that same line. One of those prints wraps, and
    its first line holds a carriage return and 120 spaces and nothing else --
    so the completed transcript went to stdout unconditionally, the audit
    reported it clean, and journalctl rendered the result as "[214B blob
    data]" so looking in the journal for leaked speech found none either. A
    marketplace reviewer found it by reading the file.

    A syntax tree sees a call as one node however it is wrapped.
    """
    import ast

    src = (ROOT / "daemon" / "wake_listen.py").read_text()
    tree = ast.parse(src)
    # run_simulated is a bench entry point whose whole job is to print a
    # replayed turn to whoever ran it.
    bench_from = src.count("\n", 0, src.index("def run_simulated")) + 1

    offenders = []

    def walk(node, parents):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print" and node.lineno < bench_from):
            seg = ast.get_source_segment(src, node) or ""
            if "safe_for_terminal" in seg and not any(
                    isinstance(p, ast.If) and "speech_is_logged" in ast.dump(p.test)
                    for p in parents):
                offenders.append((node.lineno, seg.split("\n")[0].strip()[:60]))
        for child in ast.iter_child_nodes(node):
            walk(child, [node] + parents)

    walk(tree, [])
    assert not offenders, \
        "these prints put speech on stdout with no gate: " + repr(offenders)


def test_the_structural_lines_are_never_gated():
    """Timings, wake events, tool markers, lock transitions. Nearly every bug
    in this project was found from those rather than from the words, so the
    setting must not reach them."""
    daemon = (ROOT / "daemon" / "wake_listen.py").read_text()
    for line in ('first token', 'microphone closed',
                 "mic {'engaged' if self.enabled else 'released'}",
                 'whisper {size} ready'):
        at = daemon.index(line)
        before = daemon[max(0, at - 240):at]
        assert "speech_is_logged" not in before, \
            f"a diagnostic line got caught by the speech gate: {line}"


def test_the_setting_exists_in_all_three_places():
    import json as _json
    manifest = _json.loads((ROOT / "manifest.json").read_text())
    assert "logSpeech" in {k["key"] for k in manifest["barWidget"]["schema"]}
    assert manifest["barWidget"]["defaults"]["logSpeech"] is False, \
        "it has to be off by default or it is not a privacy setting"
    import runtime
    assert runtime.DEFAULTS["logSpeech"] is False
    assert '"logSpeech"' in (ROOT / "Settings.qml").read_text()


def test_a_whole_turn_runs_with_the_words_withheld(daemon, capsys, monkeypatch):
    """The gate sits inside the reply loop, so getting it wrong does not log
    less -- it raises mid-turn and the answer never arrives. Driven rather
    than grepped for exactly that reason.
    """
    from adapters.base import Chunk

    class Agent:
        def send(self, text, session_id=None):
            yield Chunk(text="Hello there. ")
            yield Chunk(text="Second sentence here.")

        def cancel(self):
            pass

    daemon.agent = Agent()
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    daemon.answer("say something", {"transcript": "say something"})

    out = capsys.readouterr().out
    assert "Hello there" not in out, "the reply reached a log that keeps it"
    assert daemon.speaker.said, "the turn has to still happen, not just go quiet"
