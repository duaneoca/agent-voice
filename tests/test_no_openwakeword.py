"""Installing without openWakeWord has to be a real choice, not a broken one.

install.sh asks, and --no-oww is a supported answer -- the engine default is
Vosk, and openWakeWord is another ~100MB on top of a 750MB install. So both
the daemon and the settings page have to behave when the package is absent.

The daemon's half already worked: choosing openWakeWord without it falls back
to Vosk. What did not was the settings page, which had no idea whether the
package existed. It offered the engine, and on selecting it went on showing an
openWakeWord model picker, detection threshold and verifier status while Vosk
was the thing listening.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = (ROOT / "Settings.qml").read_text()


def test_the_daemon_falls_back_rather_than_dying():
    """A missing package raises ImportError from the constructor, which is an
    Exception, so the existing handler catches it and selects Vosk."""
    src = (ROOT / "daemon" / "wake_listen.py").read_text()
    branch = src[src.index('if engine == "openwakeword":'):]
    branch = branch[:branch.index("self._reset_wake()")]
    assert "except Exception" in branch
    assert 'self.engine = "vosk"' in branch, "must select the engine it fell back to"


def test_the_engine_default_does_not_require_the_optional_package():
    """Otherwise --no-oww installs something that cannot start."""
    import sys
    sys.path.insert(0, str(ROOT / "daemon"))
    from runtime import DEFAULTS
    assert DEFAULTS["engine"] == "vosk"

    import json
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["barWidget"]["defaults"]["engine"] == "vosk", \
        "the manifest default must agree, or the widget and daemon disagree"


def test_settings_knows_whether_openwakeword_is_installed():
    assert "owwAvailable" in SETTINGS
    assert "site-packages/openwakeword" in SETTINGS, "must probe for the package"
    assert "scanOww.running = true" in SETTINGS, "the probe has to actually run"


def test_engine_specific_controls_follow_what_is_running_not_what_is_set():
    """`usingOww` is the setting; `owwLive` is the setting and the package.

    Keying the controls off the setting alone described an engine that was not
    running. Nothing engine-specific may use the bare setting for visibility.
    """
    assert "readonly property bool owwLive: usingOww && owwAvailable" in SETTINGS

    # The one legitimate use of the bare setting is the banner about having
    # selected an engine that is not installed -- that state is exactly "set
    # but not live". Anything else keying off the setting is describing an
    # engine that may not be running.
    bare = [l.strip() for l in SETTINGS.splitlines()
            if re.match(r"visible: !?root\.usingOww\s*$", l.strip())]
    assert bare == [], bare
    assert SETTINGS.count("visible: root.owwLive") >= 6


def test_the_not_installed_state_is_visible_somewhere():
    """It used to have no representation at all: the page looked identical to a
    working openWakeWord install while Vosk answered."""
    assert "visible: root.usingOww && !root.owwAvailable" in SETTINGS
    assert "openWakeWord is not installed" in SETTINGS


def test_nothing_offers_to_switch_to_an_engine_that_is_not_there():
    """Switching to a missing engine changes nothing a user can see -- the
    daemon falls back again -- so the offer must be to install it."""
    assert "Install openWakeWord to enable this" in SETTINGS
    assert "Install openWakeWord…" in SETTINGS


def test_the_installer_is_called_with_flags_it_accepts():
    """install.sh rejects an unknown option with exit 2, and the first draft of
    this passed a --no-voice-change that does not exist."""
    accepted = set()
    for m in re.finditer(r"^\s*--([a-z-]+)(?:\|--?([a-z-]+))*\)",
                         (ROOT / "install.sh").read_text(), re.M):
        accepted.update(g for g in m.groups() if g)
    assert "oww" in accepted, f"parsed flags look wrong: {accepted}"

    for call in re.findall(r"/install\.sh([^\"']*)", SETTINGS):
        for flag in re.findall(r"--([a-z-]+)", call):
            assert flag in accepted, f"install.sh does not accept --{flag}"


def test_the_training_section_is_openwakeword_only():
    """A Colab notebook, a .onnx to drop in, and a phrase list this engine
    ships -- none of it applies to Vosk, which takes any phrase and needs no
    training, yet all of it was on screen while Vosk was selected."""
    start = SETTINGS.index('text: "TRAINING YOUR OWN PHRASE"')
    section = SETTINGS[start:SETTINGS.index("// --- input ---")]
    children = re.findall(r"^            (Text|Button|PanelSeparator) [{]", section, re.M)
    guarded = section.count("visible: root.owwLive")
    assert guarded >= len(children), \
        f"{len(children)} children, only {guarded} guarded"


# --- optional means not asked for ------------------------------------------

def test_the_install_does_not_prompt_for_openwakeword():
    """It asked during every fresh install, with gum's affirmative preselected.

    An optional component presented as a blocking question with Yes already
    chosen is a demand wearing a question mark. It belongs where someone goes
    looking for it, which is the settings page.
    """
    script = (ROOT / "install.sh").read_text()
    asks = [l.strip() for l in script.splitlines()
            if re.search(r"^\s*(if .*)?\bask \"", l)]
    assert len(asks) == 1, f"expected only the uninstall confirm, got {asks}"
    assert "Remove the agentvoice" in asks[0]


def test_openwakeword_is_off_unless_asked_for_or_already_selected():
    """Three ways it should install: --oww, --yes --oww, or settings that
    already name it -- because then omitting it gives a daemon configured for
    an engine it cannot start. Not: a bare install, and not --yes alone."""
    script = (ROOT / "install.sh").read_text()
    assert "WANT_OWW=0" in script, "the default has to be off"
    forced = script[script.index("# Settings still override the default"):]
    forced = forced[:forced.index("Wake word: Vosk")]
    assert "engine // empty" in forced, "must read the configured engine"
    assert "WANT_OWW=1" in forced, "and install it when that engine is chosen"

    # --yes must no longer imply it: that only made sense while there was a
    # prompt for --yes to answer.
    assert "(( ASSUME_YES )); then\n  WANT_OWW=1" not in script


def test_the_install_says_where_to_get_it():
    script = (ROOT / "install.sh").read_text()
    assert "--oww" in script
    assert "Engine in the Agent Voice settings" in script


def test_speech_comes_before_the_optional_endpoint():
    """The endpoint is optional and belongs last; speech is not.

    Asserted because the two have been reordered twice by hand and the comment
    introducing the endpoint block still said "speech" from the first move.
    """
    order = [m.group(1) for m in re.finditer(
        r'text: "(PROJECT|WAKE WORD|INPUT|TIMING|SPEECH|ENDPOINT \(optional\))"',
        SETTINGS)]
    assert order.index("SPEECH") < order.index("ENDPOINT (optional)")
    assert order.index("ENDPOINT (optional)") == len(order) - 1, \
        "the endpoint section should be the last one on the page"
