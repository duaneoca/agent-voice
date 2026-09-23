"""Permission levels, and the path rule that makes "edits" safe.

This file exists because the failure here is silent and one-directional: a
bug that denies too much is annoying, a bug that allows too much is the thing
the whole permission system was built to prevent. Every case below is written
from the allowing side -- what could talk this into saying yes.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import permission_hook
from permission_hook import _inside, decide


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An 'edits' level pointed at a real directory."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.txt").write_text("s\n")
    monkeypatch.setattr(permission_hook, "_settings", lambda: ("edits", root))
    return root, outside


class TestInside:
    def test_a_file_in_the_project(self, project):
        root, _ = project
        assert _inside(str(root / "src" / "app.py"), root)

    def test_a_file_that_does_not_exist_yet(self, project):
        """Write creates files; the rule still has to judge them."""
        root, _ = project
        assert _inside(str(root / "src" / "new.py"), root)

    def test_a_file_outside(self, project):
        root, outside = project
        assert not _inside(str(outside / "secret.txt"), root)

    def test_dot_dot_does_not_escape(self, project):
        """The reason both sides are resolved."""
        root, outside = project
        assert not _inside(str(root / ".." / "elsewhere" / "secret.txt"), root)

    def test_a_symlink_pointing_out_does_not_escape(self, project):
        root, outside = project
        link = root / "src" / "link.txt"
        link.symlink_to(outside / "secret.txt")
        assert not _inside(str(link), root)

    def test_a_sibling_with_the_same_prefix(self, project):
        """String prefixes would call /tmp/x/project-evil part of /tmp/x/project."""
        root, _ = project
        assert not _inside(str(root.parent / (root.name + "-evil") / "f"), root)

    def test_empty_and_nonsense_are_not_inside(self, project):
        root, _ = project
        assert not _inside("", root)
        assert not _inside("\x00", root)


class TestLevels:
    def test_read_only_tools_never_prompt_at_any_level(self, monkeypatch):
        for level in ("ask", "edits", "trusted"):
            monkeypatch.setattr(permission_hook, "_settings",
                                lambda lv=level: (lv, Path("/")))
            assert decide("Read", {"file_path": "/etc/passwd"})[0] == "allow"

    def test_trusted_allows_without_asking(self, monkeypatch):
        monkeypatch.setattr(permission_hook, "_settings",
                            lambda: ("trusted", Path("/nowhere")))
        assert decide("Bash", {"command": "rm -rf /"})[0] == "allow"

    def test_edits_allows_a_file_in_the_project(self, project):
        root, _ = project
        assert decide("Edit", {"file_path": str(root / "src" / "app.py")})[0] == "allow"

    def test_edits_does_not_allow_bash(self, project, monkeypatch):
        """Bash has no file_path, so no path rule can vouch for it."""
        root, _ = project
        monkeypatch.setenv("AGENTVOICE_PERMISSION_TIMEOUT", "0.3")
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.3)
        assert decide("Bash", {"command": "rm -rf ~"})[0] == "deny"

    def test_edits_does_not_allow_a_file_outside_the_project(self, project, monkeypatch):
        root, outside = project
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.3)
        assert decide("Write", {"file_path": str(outside / "secret.txt")})[0] == "deny"

    def test_a_missing_file_path_is_not_an_allow(self, project, monkeypatch):
        """An absent key must not read as 'inside the project'."""
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.3)
        assert decide("Write", {})[0] == "deny"


def test_unreadable_config_falls_back_to_asking(monkeypatch):
    """A config this cannot parse must not become a reason to stop asking."""
    import runtime

    def boom(*a, **k):
        raise RuntimeError("config on fire")

    monkeypatch.setattr(runtime, "Config", boom)
    level, _ = permission_hook._settings()
    assert level == "ask"
