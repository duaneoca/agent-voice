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
        root, _ = project
        assert not _inside(str(root / ".." / "elsewhere" / "secret.txt"), root)

    def test_a_symlink_pointing_out_does_not_escape(self, project):
        root, outside = project
        link = root / "src" / "link.txt"
        link.symlink_to(outside / "secret.txt")
        assert not _inside(str(link), root)

    def test_a_dangling_symlink_pointing_out_does_not_escape(self, project):
        """The one `exists()` used to wave through.

        A link whose target is not there yet is how a cloned repo escapes:
        `notes.txt -> ~/.config/systemd/user/x.service` does not exist, so
        only its parent got resolved and the link read as inside the project.
        Write would then create the *target*.
        """
        root, outside = project
        link = root / "src" / "dangling.txt"
        link.symlink_to(outside / "not-there-yet.service")
        assert not _inside(str(link), root)

    def test_no_project_directory_is_not_inside_anything(self):
        """scope_root answers None when nothing is configured, and None has
        to mean "no silent writes", not "everywhere"."""
        assert not _inside("/home/someone/notes.txt", None)

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
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.3)
        assert decide("Bash", {"command": "rm -rf ~"})[0] == "deny"

    def test_edits_does_not_allow_a_file_outside_the_project(self, project, monkeypatch):
        _, outside = project
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.3)
        assert decide("Write", {"file_path": str(outside / "secret.txt")})[0] == "deny"

    def test_a_missing_file_path_is_not_an_allow(self, project, monkeypatch):
        """An absent key must not read as 'inside the project'."""
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.3)
        assert decide("Write", {})[0] == "deny"


class TestEditsStillAsksAboutCodeThatRuns:
    """"edits" is a statement about source files.

    Everything below is inside the project and would pass the path rule, and
    every one of them is a way to make something else run on the next turn.
    One granted edit must not become arbitrary code.
    """

    @pytest.fixture(autouse=True)
    def quick(self, monkeypatch):
        monkeypatch.setattr(permission_hook, "TIMEOUT", 0.2)

    @pytest.mark.parametrize("rel", [
        ".claude/settings.json",
        ".mcp.json",
        ".envrc",
        ".git/hooks/pre-commit",
        ".vscode/tasks.json",
        ".gemini/settings.json",
        "verifiers/hey_claude.joblib",
    ])
    def test_it_asks_about_a_file_that_executes(self, project, rel):
        root, _ = project
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        assert decide("Write", {"file_path": str(target)})[0] == "deny"

    def test_it_still_allows_an_ordinary_source_file(self, project):
        """The denylist must not have quietly turned "edits" into "ask"."""
        root, _ = project
        assert decide("Write", {"file_path": str(root / "src" / "new.py")})[0] == "allow"


class TestScopeRoot:
    """What "edits" is allowed to mean at all.

    project_dir answers "where should the agent run" and says home when
    nothing is set, which is right for a shell and wrong for a write grant:
    home holds .bashrc, the systemd user units, and shell.json -- and
    shell.json holds the permission level, so an agent that could write it
    unasked could make itself "trusted".
    """

    def test_nothing_configured_means_no_scope(self):
        from paths import scope_root
        assert scope_root("") is None
        assert scope_root("   ") is None

    def test_home_itself_is_refused(self):
        from paths import scope_root
        assert scope_root(str(Path.home())) is None
        assert scope_root("~") is None

    def test_the_root_directory_is_refused(self):
        from paths import scope_root
        assert scope_root("/") is None

    def test_a_vanished_directory_narrows_rather_than_widens(self, tmp_path):
        """A typo'd or unmounted path used to fall back to home, i.e. to a
        wider grant than the one that was configured."""
        from paths import scope_root
        assert scope_root(str(tmp_path / "not-there")) is None

    def test_a_real_project_is_the_scope(self, tmp_path):
        from paths import scope_root
        (tmp_path / "work").mkdir()
        assert scope_root(str(tmp_path / "work")) == (tmp_path / "work").resolve()


def test_edits_with_no_project_configured_behaves_as_ask(monkeypatch, tmp_path):
    """The default install has projectDir = "", and that must not read as
    "all of home is editable"."""
    monkeypatch.setattr(permission_hook, "_settings", lambda: ("edits", None))
    monkeypatch.setattr(permission_hook, "TIMEOUT", 0.2)
    assert decide("Write", {"file_path": str(tmp_path / "f.txt")})[0] == "deny"


def test_unreadable_config_falls_back_to_asking(monkeypatch):
    """A config this cannot parse must not become a reason to stop asking."""
    import runtime

    def boom(*a, **k):
        raise RuntimeError("config on fire")

    monkeypatch.setattr(runtime, "Config", boom)
    level, _ = permission_hook._settings()
    assert level == "ask"
