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
        assert Config().get_str("engine") == DEFAULTS["engine"]

    def test_shell_json_wins(self, files):
        shell, _ = files
        write_widget(shell, engine="openwakeword")
        c = Config()
        assert c.get_str("engine") == "openwakeword"
        assert "shell.json" in c.source

    def test_toml_is_the_fallback_without_omarchy(self, files):
        _, toml = files
        toml.write_text('[wake]\nphrase = "hey machine"\n')
        c = Config()
        assert c.get_str("phrase") == "hey machine"
        assert c.source == "agentvoice.toml"

    def test_shell_json_beats_toml(self, files):
        shell, toml = files
        toml.write_text('[wake]\nphrase = "from toml"\n')
        write_widget(shell, phrase="from shell json")
        assert Config().get_str("phrase") == "from shell json"

    def test_a_widget_that_is_not_ours_is_ignored(self, files):
        shell, _ = files
        shell.write_text(json.dumps({"bar": {"layout": {"right": [
            {"id": "omarchy.clock", "engine": "nonsense"}]}}}))
        assert Config().get_str("engine") == DEFAULTS["engine"]

    def test_malformed_shell_json_falls_back_rather_than_crashing(self, files):
        shell, _ = files
        shell.write_text("{ this is not json")
        assert Config().get_str("engine") == DEFAULTS["engine"]


class TestReload:
    def test_reports_no_change_when_nothing_moved(self, files):
        c = Config()
        assert c.reload() is False

    def test_notices_a_changed_file(self, files):
        shell, _ = files
        c = Config()
        write_widget(shell, engine="openwakeword")
        assert c.reload() is True
        assert c.get_str("engine") == "openwakeword"


class TestPercentConversion:
    def test_the_schema_percent_becomes_the_float_the_code_reads(self, files):
        # The bar schema has no float type, so confidence travels as a whole
        # percent and is converted on the way out.
        shell, _ = files
        write_widget(shell, wakeConfidencePct=81)
        assert Config()["wakeConfidence"] == pytest.approx(0.81)

    @pytest.mark.parametrize("key,junk,want", [
        ("leadInMs", "abc", DEFAULTS["leadInMs"]),
        ("leadInMs", None, DEFAULTS["leadInMs"]),
        ("leadInMs", [1, 2], DEFAULTS["leadInMs"]),
        ("micThresholdDb", {}, DEFAULTS["micThresholdDb"]),
        ("wakeConfidencePct", "seventy", DEFAULTS["wakeConfidencePct"]),
    ])
    def test_a_junk_value_falls_back_rather_than_crashing(self, files, key,
                                                          junk, want):
        """These are read from inside the run loop, where a ValueError is
        the daemon stopping -- and shell.json is hand-edited and synced
        between machines, so a string where a number belongs happens."""
        shell, _ = files
        write_widget(shell, **{key: junk})
        assert Config().get_int(key) == want

    def test_a_junk_confidence_still_yields_a_usable_float(self, files):
        shell, _ = files
        write_widget(shell, wakeConfidencePct="seventy")
        assert Config()["wakeConfidence"] == DEFAULTS["wakeConfidence"]

    @pytest.mark.parametrize("written,want", [
        ("true", True), ("True", True), ("yes", True), ("on", True),
        ("false", False), ("no", False), ("", False), ("anything", False),
    ])
    def test_a_boolean_written_as_a_string_is_read_as_one(self, files,
                                                          written, want):
        """`omarchy bar set` without --json writes a bare string, and
        bool("false") is True, which is the wrong answer for every knob."""
        shell, _ = files
        write_widget(shell, speakReplies=written)
        assert Config().get_bool("speakReplies") is want

    def test_a_float_in_the_toml_reaches_the_code_that_reads_a_percent(self, files):
        """It did not. The file writes `wake_confidence = 0.80`, every caller
        reads `wakeConfidence`, and __getitem__ answers that from
        `wakeConfidencePct` -- which is always present from DEFAULTS. So the
        value was read, merged, and then ignored."""
        _, toml = files
        toml.write_text("[audio]\nwake_confidence = 0.80\n")
        cfg = Config()
        assert cfg["wakeConfidence"] == pytest.approx(0.80)

    def test_a_percent_in_shell_json_still_wins_over_the_toml(self, files):
        shell, toml = files
        toml.write_text("[audio]\nwake_confidence = 0.80\n")
        write_widget(shell, wakeConfidencePct=60)
        assert Config()["wakeConfidence"] == pytest.approx(0.60)

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
