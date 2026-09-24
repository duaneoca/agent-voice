"""Where things live, and the dev-versus-installed fallback."""
from __future__ import annotations

from pathlib import Path

import importlib.util

import pytest

import paths


def test_every_location_is_under_xdg_not_the_repo():
    # Anything downloaded must survive a git pull without showing up in
    # git status.
    for p in (paths.DATA_DIR, paths.CONFIG_DIR, paths.RUNTIME_DIR,
              paths.VENV, paths.MODELS, paths.VERIFIERS, paths.WAKEWORDS):
        assert paths.ROOT not in p.parents, f"{p} is inside the repo"


def test_root_is_the_plugin_directory():
    # The repo root doubles as the plugin dir, so the manifest must sit there.
    assert (paths.ROOT / "manifest.json").exists()
    assert (paths.ROOT / "daemon").is_dir()


def test_vocab_prefers_a_user_override(tmp_path, monkeypatch):
    user = tmp_path / "vocab.txt"
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path)
    assert paths.vocab_file().name == "vocab.txt"
    assert paths.vocab_file() != user          # absent, so the shipped one
    user.write_text("Omarchy\n")
    assert paths.vocab_file() == user          # present, so theirs


def test_models_fall_back_to_the_bench_tree_in_a_checkout(monkeypatch, tmp_path):
    # An installed copy has no bench/; a development one shares its download.
    monkeypatch.setattr(paths, "MODELS", tmp_path / "empty")
    monkeypatch.setattr(paths, "_DEV_MODELS", tmp_path / "bench")
    (tmp_path / "bench").mkdir()
    assert paths.models_dir() == tmp_path / "bench"


def test_config_toml_prefers_a_user_override(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path)
    shipped = paths.config_toml()
    (tmp_path / "agentvoice.toml").write_text("")
    assert paths.config_toml() != shipped


class TestSuiteIsolation:
    """The suite must not read the machine it runs on.

    This exists because it did. `SHELL_JSON` was `Path.home() / ".config/..."`,
    which conftest's XDG redirection could not reach, so the permission tests
    answered according to whatever level was set on the developer's desktop --
    and `decide("Bash", {"command": "rm -rf /"})` returned "allow" on a run
    that reported 104 passed.
    """

    def test_shell_json_is_not_the_real_one(self):
        import runtime
        real = Path.home() / ".config/omarchy/shell.json"
        assert runtime.SHELL_JSON != real

    def test_a_subprocess_also_sees_the_sandbox(self):
        """In-process patching does not reach the hook, which Claude spawns."""
        import subprocess
        import sys

        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from paths import SHELL_JSON; print(SHELL_JSON)"
             % str(Path(__file__).resolve().parent.parent / "daemon")],
            capture_output=True, text=True, check=True).stdout.strip()
        assert out != str(Path.home() / ".config/omarchy/shell.json")


class TestCustomWakeWords:
    """A phrase you trained yourself must be usable everywhere the four in
    the wheel are.

    The settings screen has always listed models from
    ~/.local/share/agentvoice/wakewords in its dropdown, and resolve_model
    did not know the directory existed. So choosing one failed, and the
    verifier trainer would not offer it either -- which is the combination
    the README actively encourages, since it links to a training notebook.
    """

    @pytest.fixture
    def custom(self, monkeypatch, tmp_path):
        import verifier
        wakewords = tmp_path / "wakewords"
        wakewords.mkdir()
        (wakewords / "hey_claude.onnx").write_bytes(b"not a real model")
        monkeypatch.setattr(verifier, "WAKEWORDS", wakewords)
        return verifier

    def test_a_trained_phrase_resolves(self, custom):
        path, key = custom.resolve_model("hey_claude")
        assert path.name == "hey_claude.onnx"
        assert key == "hey_claude", "the verifier is keyed by this exact stem"

    @pytest.mark.skipif(importlib.util.find_spec("openwakeword") is None,
                        reason="the bundled models ship with openWakeWord, "
                               "which the test runner deliberately lacks")
    def test_a_bundled_phrase_still_resolves(self, custom):
        path, key = custom.resolve_model("hey_jarvis")
        assert key.startswith("hey_jarvis")

    def test_yours_wins_over_a_bundled_name(self, custom):
        """If you trained one called hey_jarvis, you meant yours."""
        (custom.WAKEWORDS / "hey_jarvis.onnx").write_bytes(b"mine")
        path, key = custom.resolve_model("hey_jarvis")
        assert path.parent == custom.WAKEWORDS
        assert key == "hey_jarvis"

    def test_an_unknown_name_says_where_it_looked(self, custom):
        """Including when openWakeWord is not installed to look in."""
        with pytest.raises(custom.ClipProblem) as caught:
            custom.resolve_model("hey_nonsuch")
        message = str(caught.value)
        assert "wakewords" in message
        assert "resources/models" in message or "not installed" in message

    def test_yours_resolves_without_openwakeword_at_all(self, custom, monkeypatch):
        """A trained phrase must not need the optional extra to be importable:
        this is what broke CI, which installs pytest and nothing else."""
        import builtins
        real = builtins.__import__

        def refuse(name, *a, **k):
            if name == "openwakeword":
                raise ImportError("no openwakeword here")
            return real(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", refuse)
        path, key = custom.resolve_model("hey_claude")
        assert key == "hey_claude"
