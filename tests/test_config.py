"""Where a setting comes from, and what happens when it changes.

Two of the worst bugs so far lived here: a value the daemon read once and
never again, and a key the schema exposed that the defaults did not know.
"""
from __future__ import annotations

import json

import pytest

import runtime
from runtime import DEFAULTS, Config


@pytest.fixture
def files(tmp_path, monkeypatch):
    """Point Config at scratch files instead of the real ones."""
    shell = tmp_path / "shell.json"
    toml = tmp_path / "agentvoice.toml"
    monkeypatch.setattr(runtime, "SHELL_JSON", shell)
    monkeypatch.setattr(runtime, "config_toml", lambda: toml)
    return shell, toml


def write_widget(path, **settings):
    path.write_text(json.dumps({"bar": {"layout": {"right": [
        {"id": "omarchy.clock"},
        {"id": "duaneoca.agentvoice", **settings},
    ]}}}))


class TestPrecedence:
    def test_defaults_when_nothing_is_configured(self, files):
        assert Config().str("engine") == DEFAULTS["engine"]

    def test_shell_json_wins(self, files):
        shell, _ = files
        write_widget(shell, engine="openwakeword")
        c = Config()
        assert c.str("engine") == "openwakeword"
        assert "shell.json" in c.source

    def test_toml_is_the_fallback_without_omarchy(self, files):
        _, toml = files
        toml.write_text('[wake]\nphrase = "hey machine"\n')
        c = Config()
        assert c.str("phrase") == "hey machine"
        assert c.source == "agentvoice.toml"

    def test_shell_json_beats_toml(self, files):
        shell, toml = files
        toml.write_text('[wake]\nphrase = "from toml"\n')
        write_widget(shell, phrase="from shell json")
        assert Config().str("phrase") == "from shell json"

    def test_a_widget_that_is_not_ours_is_ignored(self, files):
        shell, _ = files
        shell.write_text(json.dumps({"bar": {"layout": {"right": [
            {"id": "omarchy.clock", "engine": "nonsense"}]}}}))
        assert Config().str("engine") == DEFAULTS["engine"]

    def test_malformed_shell_json_falls_back_rather_than_crashing(self, files):
        shell, _ = files
        shell.write_text("{ this is not json")
        assert Config().str("engine") == DEFAULTS["engine"]


class TestReload:
    def test_reports_no_change_when_nothing_moved(self, files):
        c = Config()
        assert c.reload() is False

    def test_notices_a_changed_file(self, files):
        shell, _ = files
        c = Config()
        write_widget(shell, engine="openwakeword")
        assert c.reload() is True
        assert c.str("engine") == "openwakeword"


class TestPercentConversion:
    def test_the_schema_percent_becomes_the_float_the_code_reads(self, files):
        # The bar schema has no float type, so confidence travels as a whole
        # percent and is converted on the way out.
        shell, _ = files
        write_widget(shell, wakeConfidencePct=81)
        assert Config()["wakeConfidence"] == pytest.approx(0.81)

    def test_falls_back_to_the_float_default(self, files):
        assert Config()["wakeConfidence"] == pytest.approx(DEFAULTS["wakeConfidence"])


class TestSchemaAgreement:
    def test_every_manifest_key_has_a_default(self):
        # The check script asserts this too; having it here means it fails in
        # the same run as everything else.
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        schema = json.loads((root / "manifest.json").read_text())["barWidget"]["schema"]
        missing = [k["key"] for k in schema if k["key"] not in DEFAULTS]
        assert not missing, f"no daemon default for {missing}"

    def test_manifest_defaults_agree_with_daemon_defaults(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        bw = json.loads((root / "manifest.json").read_text())["barWidget"]
        clashes = {k: (v, DEFAULTS[k]) for k, v in bw["defaults"].items()
                   if k in DEFAULTS and DEFAULTS[k] != v}
        assert not clashes, f"manifest and daemon disagree: {clashes}"
