"""Where things live, and the dev-versus-installed fallback."""
from __future__ import annotations

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
