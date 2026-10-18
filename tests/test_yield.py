"""Lending the microphone to whatever else is recording.

Measured before it was built: dictating into the editor and saying the wake
phrase made the agent answer out loud *into the dictation* -- a wake at
17:34:05 against a capture stream that opened at 17:33:57. PipeWire does not
arbitrate, so both streams run side by side quite happily; this is a courtesy,
not a fix for contention. The eight seconds between the two is also why the
existing 2s poll is fast enough to have prevented it.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
DAEMON = (ROOT / "daemon" / "wake_listen.py").read_text()


def _graph(objs):
    """A pw-dump reply, as the real one is shaped."""
    return json.dumps(objs)


def _client(cid, pid):
    return {"id": cid, "type": "PipeWire:Interface:Client",
            "info": {"props": {"application.process.id": pid}}}


def _stream(cid, name, state="running"):
    return {"id": cid + 100, "type": "PipeWire:Interface:Node",
            "info": {"state": state,
                     "props": {"media.class": "Stream/Input/Audio",
                               "client.id": cid,
                               "application.name": name}}}


class TestDetection:
    @staticmethod
    def _run(monkeypatch, payload, rc=0):
        import wake_listen

        class Done:
            stdout = payload
            returncode = rc

        monkeypatch.setattr(wake_listen.subprocess, "run",
                            lambda *a, **k: Done())
        return wake_listen.other_recorders(4242)

    def test_our_own_stream_is_not_another_app(self, monkeypatch):
        """The pid is the only reliable way to tell: `agentvoice monitor`
        opens a stream with exactly the same application.name as the daemon."""
        assert self._run(monkeypatch, _graph(
            [_client(1, 4242), _stream(1, "PipeWire ALSA [python3.13]")])) == []

    def test_someone_else_recording_is_reported_by_name(self, monkeypatch):
        got = self._run(monkeypatch, _graph(
            [_client(1, 4242), _stream(1, "PipeWire ALSA [python3.13]"),
             _client(2, 4930), _stream(2, "PipeWire ALSA [code]")]))
        assert got == ["PipeWire ALSA [code]"]

    def test_a_client_declared_after_its_node_still_resolves(self, monkeypatch):
        """pw-dump does not order the graph for us."""
        assert self._run(monkeypatch, _graph(
            [_stream(2, "recorder"), _client(2, 9999),
             _stream(1, "ours"), _client(1, 4242)])) == ["recorder"]

    def test_an_idle_capture_node_does_not_count(self, monkeypatch):
        """A browser with a tab open keeps idle capture nodes around. Treating
        those as recording would mute the wake word permanently."""
        assert self._run(monkeypatch, _graph(
            [_client(2, 9999), _stream(2, "firefox", state="idle")]) ) == []

    def test_playback_is_not_recording(self, monkeypatch):
        import wake_listen

        class Done:
            stdout = json.dumps([
                {"id": 7, "type": "PipeWire:Interface:Node",
                 "info": {"state": "running",
                          "props": {"media.class": "Stream/Output/Audio",
                                    "client.id": 7}}}])
            returncode = 0

        monkeypatch.setattr(wake_listen.subprocess, "run", lambda *a, **k: Done())
        assert wake_listen.other_recorders(4242) == []

    @pytest.mark.parametrize("payload", ["", "not json", "{}", "[1, 2, 3]"])
    def test_nonsense_is_undetermined_rather_than_nobody(self, monkeypatch, payload):
        """None, not []. Answering "nobody" would reopen the microphone over
        somebody else's recording."""
        assert self._run(monkeypatch, payload) is None

    def test_a_missing_pw_dump_is_undetermined(self, monkeypatch):
        import wake_listen
        monkeypatch.setattr(wake_listen.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError))
        assert wake_listen.other_recorders(4242) is None

    def test_a_hang_is_undetermined(self, monkeypatch):
        import wake_listen

        def boom(*a, **k):
            raise subprocess.TimeoutExpired("pw-dump", 5)

        monkeypatch.setattr(wake_listen.subprocess, "run", boom)
        assert wake_listen.other_recorders(4242) is None

    def test_it_asks_with_a_timeout(self):
        """A pw-dump that never returns must not stop the audio loop."""
        body = DAEMON[DAEMON.index("def other_recorders"):]
        body = body[:body.index("\ndef ")]
        assert "timeout=" in body


class TestTheFlag:
    def test_listening_needs_all_three(self, daemon):
        for enabled in (True, False):
            for locked in (True, False):
                for yielded in (True, False):
                    daemon.enabled, daemon.locked, daemon.yielded = (
                        enabled, locked, yielded)
                    assert daemon.listening is (
                        enabled and not locked and not yielded)

    def test_it_never_writes_what_the_user_asked_for(self, daemon):
        """The same rule as the lock, for the same reason: writing `enabled`
        here would turn the microphone back *on*, when the other app stopped,
        for somebody who had deliberately muted it."""
        body = DAEMON[DAEMON.index("def reconcile_yield"):]
        body = body[:body.index("\n    def ")]
        assert "self.enabled = " not in body

    def test_a_lock_outranks_a_borrowed_microphone(self, daemon):
        """Both true is possible -- a recorder can be running when the screen
        locks -- and the lock is the one that matters."""
        daemon.enabled = True
        daemon.locked = True
        daemon.yielded = True
        daemon._publish()
        assert json.loads(daemon.state.path.read_text())["state"] == "locked"

    def test_it_is_its_own_published_state(self, daemon):
        daemon.enabled, daemon.locked, daemon.yielded = True, False, True
        daemon._publish()
        assert json.loads(daemon.state.path.read_text())["state"] == "yielded"

    def test_a_released_microphone_is_not_reported_as_borrowed(self, daemon):
        """Muted beats borrowed in the other direction: the user turned it off,
        and saying "another app is recording" would misattribute that."""
        daemon.enabled, daemon.locked, daemon.yielded = False, False, True
        daemon._publish()
        assert json.loads(daemon.state.path.read_text())["state"] == "off"


class TestTheLoop:
    def test_the_poll_is_above_the_gate(self):
        """The exact bug the lock gate shipped: once the microphone is lent
        out the loop stops at the `listening` gate, so a check placed after it
        never runs again and the microphone stays lent forever."""
        run = DAEMON[DAEMON.index("    def run("):]
        assert run.index("self.reconcile_yield()") < run.index("if not self.listening:")

    def test_it_stops_a_reply_in_progress(self):
        """That audio is what lands in somebody's dictation, so it is the
        whole point rather than a nicety."""
        body = DAEMON[DAEMON.index("def reconcile_yield"):]
        body = body[:body.index("\n    def ")]
        assert "self.silence()" in body

    def test_not_being_able_to_tell_is_said_out_loud(self, daemon, monkeypatch,
                                                    capsys):
        import wake_listen
        monkeypatch.setattr(wake_listen, "other_recorders", lambda _p: None)
        daemon.enabled, daemon.locked = True, False
        daemon.reconcile_yield()
        said = capsys.readouterr().out
        assert "cannot tell" in said
        assert daemon.yielded is False, "it must not guess either way"

    def test_it_says_so_once_rather_than_every_poll(self, daemon, monkeypatch,
                                                   capsys):
        import wake_listen
        monkeypatch.setattr(wake_listen, "other_recorders", lambda _p: None)
        daemon.enabled, daemon.locked = True, False
        for _ in range(5):
            daemon.reconcile_yield()
        assert capsys.readouterr().out.count("cannot tell") == 1

    def test_turning_the_setting_off_hands_the_microphone_back(self, daemon,
                                                               monkeypatch):
        """Otherwise it would stay lent out until the other app happened to
        stop, with the setting that caused it already switched off."""
        daemon.enabled, daemon.yielded = True, True
        monkeypatch.setitem(daemon.cfg._values, "yieldToRecorders", False)
        daemon.reconcile_yield()
        assert daemon.yielded is False


class TestTheSetting:
    def test_it_exists_in_all_three_places(self):
        manifest = json.loads((ROOT / "manifest.json").read_text())
        declared = {k["key"] for k in manifest["barWidget"]["schema"]}
        assert "yieldToRecorders" in declared
        assert "yieldToRecorders" in manifest["barWidget"]["defaults"]

        import runtime
        assert "yieldToRecorders" in runtime.DEFAULTS
        assert '"yieldToRecorders"' in (ROOT / "Settings.qml").read_text()

    def test_the_panel_says_it_comes_back_on_its_own(self):
        """Unlike a mute, there is nothing to click: telling someone to click
        the microphone here would be advice that does nothing."""
        panel = (ROOT / "Panel.qml").read_text()
        assert "ANOTHER APP IS RECORDING" in panel
        assert "Listening again as soon as it stops." in panel


class TestAnotherAppStartingMidReply:
    """The case the setting was asked for: "I was about to talk to you and I
    have to remember to mute the agent voice first, otherwise it might
    respond." If the other application starts recording while a reply is
    already being read out, stepping aside after the reply has finished is
    too late -- it talked over them for the whole of it.

    `reconcile_yield()` was only called from the run loop, which `answer()`
    blocks for the whole turn.
    """

    def test_the_reply_stops_when_another_app_starts_recording(self, daemon,
                                                               monkeypatch):
        import time as _time

        import wake_listen

        t0 = _time.perf_counter()
        # Another application appears a quarter of a second in.
        monkeypatch.setattr(
            wake_listen, "other_recorders",
            lambda pid: ["Zoom"] if _time.perf_counter() - t0 > 0.25 else [])
        monkeypatch.setattr(wake_listen, "session_locked", lambda: False)
        daemon.cfg._values["yieldToRecorders"] = True
        daemon.enabled, daemon.locked, daemon.yielded = True, False, False

        spoken = []

        class Speaker:
            name = "stub"
            cancelled = 0

            def say(self, text, watch=None):
                for _ in range(40):              # 2s of audio, in slices
                    if watch is not None and watch():
                        return
                    spoken.append(1)
                    _time.sleep(0.05)

            def cancel(self):
                pass

        daemon.speaker = Speaker()
        daemon.agent = None                      # echo path: straight to speech
        daemon.answer("read this back to me at length", {"transcript": "x"})

        assert daemon.yielded, "it never noticed the other application"
        assert len(spoken) < 40, \
            f"it talked over the other application for the whole reply: {len(spoken)}"
