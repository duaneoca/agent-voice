"""Permission levels are per agent, and absence never means permission.

The bug this closes: one global level meant that trusting Claude Code --
which can be stopped mid-call by a hook we answer -- silently handed the
same trust to whatever `omarchy default agent` was switched to next, which
may have no interception point at all. Trust is a judgement about one
program's capabilities, so it cannot survive swapping the program.
"""
from __future__ import annotations

import pytest

from adapters.claude_code import ClaudeCode
from adapters.codex import Codex
from adapters.gemini import Gemini
from runtime import DEFAULTS, Config


@pytest.fixture
def cfg():
    c = Config.__new__(Config)
    c._values = dict(DEFAULTS)
    c._stamps = ()
    c.source = "test"
    return c


class TestLevelIsPerAgent:
    def test_a_decision_about_one_agent_does_not_reach_another(self, cfg):
        cfg._values["permissions"] = {"claude": "trusted"}
        assert cfg.level_for("claude") == "trusted"
        assert cfg.level_for("codex") == "ask"

    def test_no_decision_reads_as_ask(self, cfg):
        assert cfg.level_for("claude") == "ask"

    def test_a_legacy_global_string_grants_nothing(self, cfg):
        """Upgrades must not silently carry a blanket grant into the new
        shape. The safe direction is to make the user choose again."""
        cfg._values["permissions"] = "trusted"
        assert cfg.level_for("claude") == "ask"

    @pytest.mark.parametrize("junk", ["", "yolo", "TRUSTED", None, 3, {}, []])
    def test_anything_unrecognised_reads_as_ask(self, cfg, junk):
        cfg._values["permissions"] = {"claude": junk}
        assert cfg.level_for("claude") == "ask"

    def test_an_unnamed_agent_reads_as_ask(self, cfg):
        cfg._values["permissions"] = {"": "trusted", None: "trusted"}
        assert cfg.level_for(None) == "ask"
        assert cfg.level_for("") == "ask"


class TestLevelsOffered:
    """A backend only offers what it can honour."""

    def test_only_claude_offers_edits(self):
        assert "edits" in ClaudeCode().levels
        for other in (Codex(), Gemini()):
            assert "edits" not in other.levels, other.name

    def test_every_backend_can_be_asked_or_trusted(self):
        for a in (ClaudeCode(), Codex(), Gemini()):
            assert a.levels[0] == "ask" and a.levels[-1] == "trusted"

    def test_a_backend_that_cannot_ask_says_so(self):
        """The panel must not print "asks before every change" for something
        that cannot ask anything."""
        assert "cannot ask" in Codex().posture("ask")
        assert "cannot ask" in Gemini().posture("ask")
        assert "cannot ask" not in ClaudeCode().posture("ask")

    def test_edits_never_reads_as_granted_where_it_is_not_supported(self):
        """Selecting a level a backend cannot honour must not be described as
        if it were in force."""
        assert Codex().posture("edits") == Codex().posture("ask")

    def test_trusted_is_named_plainly_everywhere(self):
        for a in (ClaudeCode(), Codex(), Gemini()):
            assert "trusted" in a.posture("trusted")
