import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import QtQuick
import qs.Commons
import qs.Ui

// Agent Voice settings, as a themed overlay.
//
// The bar panel is for glancing at state; this is for changing things. Nine
// knobs in a popup anchored to a 26px bar icon was too cramped to read, and
// the knobs that matter are ones you set once rather than watch.
//
// Every value persists through `omarchy bar set duaneoca.agentvoice <key>`,
// which writes the widget's entry in shell.json -- the same file the daemon
// watches, so a change lands on the next utterance without a restart.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property bool opened: false

  // Shares the [menu] surface tokens, so themes that style the menu style
  // this too.
  property color background: Color.menu.background
  property color foreground: Color.menu.text
  property color borderColor: Color.menu.border
  property var borderSpec: Border.surfaceSpec("menu", "border", borderColor, Math.max(1, Style.space(2)))
  property color scrim: Color.menu.scrim
  readonly property color dim: Qt.darker(foreground, 1.5)
  property string fontFamily: Style.font.menuFamily

  // --- live daemon state, for the level meter -------------------------------
  property real levelDb: -99
  property string vState: "off"

  // --- settings, read from shell.json via the CLI ---------------------------
  // An overlay is not a bar widget, so it has no `settings` property injected.
  // Reading shell.json directly keeps one source of truth.
  property var cfg: ({})
  function setting(key, fallback) {
    var v = cfg[key]
    return v === undefined || v === null ? fallback : v
  }

  readonly property var phraseOptions: [
    "hey computer", "okay computer", "hey agent", "hey claude", "okay claude",
    "hey assistant", "hey jarvis", "hey machine", "computer"
  ]
  // The four that ship in the wheel, plus anything the user has trained into
  // ~/.local/share/agentvoice/wakewords/. A custom model is the only way to
  // get a phrase openWakeWord was not shipped with.
  property var customModels: []
  readonly property var owwOptions: [
    { label: "hey jarvis",  value: "hey_jarvis" },
    { label: "alexa",       value: "alexa" },
    { label: "hey mycroft", value: "hey_mycroft" },
    { label: "hey marvin",  value: "hey_marvin" }
  ].concat(customModels)
  readonly property var engineOptions: [
    { label: "Vosk — any phrase, weaker rejection", value: "vosk" },
    { label: "openWakeWord — four phrases, better rejection", value: "openwakeword" }
  ]
  readonly property var voiceOptions: [
    { label: "Lessac — female, medium", value: "lessac-medium" },
    { label: "Lessac — female, low",    value: "lessac-low" },
    { label: "Amy — female, medium",    value: "amy-medium" },
    { label: "Ryan — male, medium",     value: "ryan-medium" },
    { label: "Joe — male, medium",      value: "joe-medium" },
    { label: "HFC — male, medium",      value: "hfc_male-medium" }
  ]
  readonly property var modelOptions: ["tiny.en", "base.en", "small.en"]

  readonly property bool conversation: setting("conversationMode", true)
  readonly property string engine: setting("engine", "vosk")
  readonly property bool usingOww: engine === "openwakeword"

  function open(payloadJson) {
    opened = true
    cfgReload.running = true
    scanCustom.running = true
    stateFile.reload()
  }
  function close() { opened = false }
  function dismiss() {
    if (shell && typeof shell.hide === "function")
      shell.hide((manifest && manifest.id) || "duaneoca.agentvoice")
    else close()
  }

  function persist(key, value, json) {
    setter.command = ["omarchy", "bar", "set", "duaneoca.agentvoice",
                      key, String(value)].concat(json ? ["--json"] : [])
    setter.running = true
  }

  Process {
    id: setter
    onExited: cfgReload.running = true
  }

  // shell.json is the store; jq pulls out just this widget's entry.
  Process {
    id: cfgReload
    command: ["bash", "-c",
      "jq -c '[.bar.layout[]?[]? | select(.id==\"duaneoca.agentvoice\")][0] // {}' " +
      "\"$HOME/.config/omarchy/shell.json\" 2>/dev/null || echo '{}'"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.cfg = JSON.parse(String(text).trim() || "{}") }
        catch (e) { root.cfg = ({}) }
      }
    }
  }

  Process { id: trainer }

  // Custom .onnx wake models the user has trained and dropped in.
  Process {
    id: scanCustom
    command: ["bash", "-c",
      "d=\"${XDG_DATA_HOME:-$HOME/.local/share}/agentvoice/wakewords\"; " +
      "[ -d \"$d\" ] && for f in \"$d\"/*.onnx; do " +
      "[ -e \"$f\" ] && basename \"$f\" .onnx; done || true"]
    stdout: StdioCollector {
      onStreamFinished: {
        var out = []
        var lines = String(text).trim().split("\n")
        for (var i = 0; i < lines.length; i++) {
          var name = lines[i].trim()
          if (name !== "") out.push({ label: name.replace(/_/g, " ") + "  (yours)", value: name })
        }
        root.customModels = out
      }
    }
  }

  FileView {
    id: stateFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000") + "/agentvoice/state"
    watchChanges: true
    printErrors: false
    onFileChanged: stateFile.reload()
    onLoadFailed: root.vState = "off"
    onLoaded: {
      try {
        var d = JSON.parse(text())
        root.vState = String(d.state || "off")
        if (d.level_db !== undefined) root.levelDb = d.level_db
      } catch (e) {}
    }
  }

  PanelWindow {
    id: window
    visible: root.opened
    color: "transparent"
    WlrLayershell.namespace: "agentvoice-settings"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore
    anchors { top: true; bottom: true; left: true; right: true }

    // Click-away closes, same as every other Omarchy overlay.
    MouseArea {
      anchors.fill: parent
      onClicked: root.dismiss()
    }

    Rectangle {
      anchors.fill: parent
      color: root.scrim
    }

    // BorderSurface *is* the card -- it draws the themed border itself, the
    // way every shipped overlay uses it.
    BorderSurface {
      id: card
      anchors.centerIn: parent
      width: Math.min(Style.space(560), parent.width - Style.gapsOut * 2)
      height: Math.min(Style.space(660), parent.height - Style.gapsOut * 2)
      radius: Style.cornerRadius
      color: root.background
      borderSpec: root.borderSpec

      // Swallow clicks so they don't reach the dismiss handler behind.
      MouseArea { anchors.fill: parent }

      Keys.onEscapePressed: root.dismiss()
      focus: root.opened

      Column {
        id: body
        anchors.fill: parent
        anchors.margins: Style.spacing.panelPadding
        spacing: Style.spacing.md

        Row {
          width: parent.width
          Text {
            text: "Agent Voice"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            width: parent.width - hint.width
          }
          Text {
            id: hint
            text: "esc to close"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        Flickable {
          id: formScroll
          width: parent.width
          height: body.height - y - Style.spacing.md
          contentWidth: width
          contentHeight: form.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds

          Column {
            id: form
            width: parent.width
            spacing: Style.spacing.md

            // --- wake word ------------------------------------------------
            PanelSectionHeader {
              text: "WAKE WORD"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Dropdown {
              width: parent.width
              showLabel: true
              label: "Engine"
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: root.engineOptions
              value: root.engine
              onChanged: function(v) { root.persist("engine", v, false) }
            }

            // One dropdown per engine rather than one that swaps its options.
            // Swapping left the previous engine's value displayed -- picking
            // "hey claude" under Vosk and then switching engines still showed
            // "hey claude", which openWakeWord has no model for.
            Dropdown {
              width: parent.width
              visible: !root.usingOww
              showLabel: true
              label: "Phrase"
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: root.phraseOptions
              value: root.setting("phrase", "hey computer")
              onChanged: function(v) { root.persist("phrase", v, false) }
            }

            Dropdown {
              width: parent.width
              visible: root.usingOww
              showLabel: true
              label: "Phrase"
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: root.owwOptions
              value: root.setting("owwModel", "hey_jarvis")
              onChanged: function(v) { root.persist("owwModel", v, false) }
            }

            Text {
              width: parent.width
              visible: root.usingOww
              wrapMode: Text.WordWrap
              text: "Only these ship pretrained. A different phrase means training " +
                    "your own model — see TRAINING YOUR OWN PHRASE below."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              visible: root.usingOww
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Detection threshold"
              unit: "%"
              description: "Measured here: the real phrase peaks near 99 and a " +
                           "phonetically similar phrase near 90, so 90 rejects " +
                           "the near miss and still fires reliably."
              value: root.setting("owwThresholdPct", 90)
              minimum: 10; maximum: 99; stepSize: 1
              onCommitted: function(v) { root.persist("owwThresholdPct", v, true) }
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              visible: !root.usingOww
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Grammar confidence"
              unit: "%"
              description: "Weak against phonetic neighbours by nature: a grammar " +
                           "must pick between the phrase and anything-else, so " +
                           "\"hey cloud\" scores as high as \"hey claude\". Switch " +
                           "engines if a phone keeps waking it."
              value: root.setting("wakeConfidencePct", 70)
              minimum: 0; maximum: 100; stepSize: 5
              onCommitted: function(v) { root.persist("wakeConfidencePct", v, true) }
            }

            // --- verifier -------------------------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }
            PanelSectionHeader {
              text: "YOUR VOICE"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "The stock wake models are speaker independent, so a podcast " +
                    "or phone saying the phrase gets through. A verifier trained " +
                    "on your voice is the only layer that knows who is talking."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            // A disabled button that does nothing when clicked reads as a bug,
            // not as a precondition -- so on Vosk there is no dead control
            // here at all. The button becomes the fix instead: it switches the
            // engine, which is the thing that was missing.
            Button {
              text: root.usingOww ? "Train a verifier from your voice…"
                                  : "Switch to openWakeWord to enable this"
              fontFamily: root.fontFamily
              onClicked: {
                if (!root.usingOww) {
                  root.persist("engine", "openwakeword", false)
                  return
                }
                trainer.command = ["omarchy-launch-floating-terminal-with-presentation",
                                   "agentvoice-train-verifier"]
                trainer.running = true
                root.dismiss()
              }
            }

            Text {
              width: parent.width
              visible: !root.usingOww
              wrapMode: Text.WordWrap
              text: "Verifiers attach to an openWakeWord model. Vosk has no model " +
                    "to attach one to, so training is unavailable while it is selected."
              color: Color.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            // --- training your own phrase ---------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }
            PanelSectionHeader {
              text: "TRAINING YOUR OWN PHRASE"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "A verifier refines an existing model; it cannot create a new " +
                    "phrase. For a phrase openWakeWord was not shipped with, train " +
                    "a model from synthetic speech — tens of thousands of Piper " +
                    "clips with augmentation and adversarial negatives. Upstream's " +
                    "Colab notebook does it in under an hour; the training code is " +
                    "not in the package and needs several gigabytes of PyTorch, so " +
                    "it is not practical on this laptop.\n\n" +
                    "Drop the resulting .onnx into " +
                    "~/.local/share/agentvoice/wakewords/ and it appears in the " +
                    "phrase list above, ready for its own verifier."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            // --- input ----------------------------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }
            PanelSectionHeader {
              text: "INPUT"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Item {
              width: parent.width
              height: Style.space(18)
              readonly property real span: 60
              readonly property real levelFrac: Math.max(0, Math.min(1, (root.levelDb + span) / span))
              readonly property real gateFrac: Math.max(0, Math.min(1, (root.setting("micThresholdDb", -38) + span) / span))
              readonly property bool gateOpen: root.levelDb >= root.setting("micThresholdDb", -38)

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width
                height: Style.space(8)
                radius: height / 2
                color: Qt.darker(root.background, 1.4)
                Rectangle {
                  width: Math.max(0, parent.width * parent.parent.levelFrac)
                  height: parent.height
                  radius: parent.radius
                  color: parent.parent.gateOpen ? root.foreground : Qt.darker(root.foreground, 2.2)
                }
              }
              Rectangle {
                x: parent.width * parent.gateFrac
                width: Math.max(2, Style.space(2))
                height: parent.height
                color: Color.urgent
              }
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Microphone gate"
              unit: " dBFS"
              description: root.usingOww
                ? "Does not affect openWakeWord, which scores every frame and " +
                  "rejects noise on its own. It still decides when your turn " +
                  "has ended."
                : "Frames quieter than this never reach the Vosk grammar, " +
                  "which would otherwise match the phrase against room noise. " +
                  "Room tone here is near -45 and speech near -24."
              value: root.setting("micThresholdDb", -38)
              minimum: -60; maximum: -20; stepSize: 1
              onCommitted: function(v) { root.persist("micThresholdDb", v, true) }
            }

            // --- timing ---------------------------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }
            PanelSectionHeader {
              text: "TIMING"; foreground: root.dim; fontFamily: root.fontFamily
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Time to start speaking"
              description: "Stops counting the moment you make a sound, so it never " +
                           "competes with the pause that ends a turn."
              value: root.setting("leadInMs", 5000)
              minimum: 1000; maximum: 15000; stepSize: 500
              onCommitted: function(v) { root.persist("leadInMs", v, true) }
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Pause that ends a turn"
              value: root.setting("trailingSilenceMs", 1200)
              minimum: 400; maximum: 4000; stepSize: 100
              onCommitted: function(v) { root.persist("trailingSilenceMs", v, true) }
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Deaf period after speaking"
              description: "One microphone beside the speakers cannot win against " +
                           "its own output, so the mic goes deaf while the voice " +
                           "talks and for this long after."
              value: root.setting("echoTailMs", 350)
              minimum: 0; maximum: 1500; stepSize: 50
              onCommitted: function(v) { root.persist("echoTailMs", v, true) }
            }

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Discard shorter than"
              value: root.setting("minUtteranceMs", 400)
              minimum: 0; maximum: 2000; stepSize: 100
              onCommitted: function(v) { root.persist("minUtteranceMs", v, true) }
            }

            // --- conversation ---------------------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }
            PanelSectionHeader {
              text: "CONVERSATION"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Toggle {
              width: parent.width
              label: "Keep listening after a reply"
              description: "Carry on without repeating the wake word. Say " +
                           "\"stop\", \"cancel that\" or \"never mind\" to end it, " +
                           "or just stay quiet."
              checked: root.conversation
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.persist("conversationMode",
                                      root.conversation ? "false" : "true", true)
            }

            KnobRow {
              width: parent.width
              visible: root.conversation
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Follow-up window"
              description: "How long it keeps listening after speaking before " +
                           "it needs the wake word again."
              value: root.setting("followUpMs", 7000)
              minimum: 2000; maximum: 20000; stepSize: 500
              onCommitted: function(v) { root.persist("followUpMs", v, true) }
            }

            // --- speech ---------------------------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }
            PanelSectionHeader {
              text: "SPEECH"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Dropdown {
              width: parent.width
              showLabel: true
              label: "Voice"
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: root.voiceOptions
              value: root.setting("voice", "lessac-medium")
              onChanged: function(v) { root.persist("voice", v, false) }
            }

            Dropdown {
              width: parent.width
              showLabel: true
              label: "Transcription model"
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: root.modelOptions
              value: root.setting("model", "tiny.en")
              onChanged: function(v) { root.persist("model", v, false) }
            }

            Item { width: 1; height: Style.space(12) }
          }
        }
      }
    }
  }
}
