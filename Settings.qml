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

  // The plugin gets one overlay entry point, so this file is both the settings
  // screen and the permission prompt. The summon payload picks.
  property string mode: "settings"
  property var request: ({})
  property int secondsLeft: 0

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
  //: Which voices are actually on disk. install.sh fetches one by default
  //: and the one the settings ask for; the rest are 60MB each and arrive
  //: only if chosen. Offering all six without saying which are present let
  //: someone pick a voice that does not exist, and the only symptom was
  //: silence with the reason in a log.
  property var voicesOnDisk: []
  Process {
    id: scanVoices
    command: ["bash", "-c",
      "d=\"${XDG_DATA_HOME:-$HOME/.local/share}/agentvoice/models/piper\"; " +
      "[ -d \"$d\" ] && for f in \"$d\"/en_US-*.onnx; do " +
      "[ -e \"$f\" ] && basename \"$f\" .onnx | sed 's/^en_US-//'; done || true"]
    stdout: StdioCollector {
      onStreamFinished: {
        var out = []
        var lines = String(text).trim().split("\n")
        for (var i = 0; i < lines.length; i++)
          if (lines[i].trim() !== "") out.push(lines[i].trim())
        root.voicesOnDisk = out
      }
    }
  }

  function voiceLabel(label, value) {
    return root.voicesOnDisk.indexOf(value) >= 0 ? label
                                                 : label + "  (not downloaded)"
  }

  readonly property var rawVoiceOptions: [
    { label: "Lessac — female, medium", value: "lessac-medium" },
    { label: "Lessac — female, low",    value: "lessac-low" },
    { label: "Amy — female, medium",    value: "amy-medium" },
    { label: "Ryan — male, medium",     value: "ryan-medium" },
    { label: "Joe — male, medium",      value: "joe-medium" },
    { label: "HFC — male, medium",      value: "hfc_male-medium" }
  ]

  readonly property var voiceOptions: {
    var out = []
    for (var i = 0; i < root.rawVoiceOptions.length; i++) {
      var v = root.rawVoiceOptions[i]
      out.push({ label: root.voiceLabel(v.label, v.value), value: v.value })
    }
    return out
  }
  readonly property var modelOptions: ["tiny.en", "base.en", "small.en"]

  readonly property bool conversation: setting("conversationMode", true)
  // Published by the daemon, because what a level means depends on what the
  // running agent can honour and only the daemon knows that.
  property string vAgent: ""
  property string permissionLevel: "ask"
  property string vPosture: ""
  property var agentLevels: ["ask", "trusted"]
  property bool vRemembers: true
  property string vOverriding: ""
  property bool vGoverned: true
  property bool vVerifier: false

  //: The phrase actually being listened for. Both engines keep their own
  //: key and the inactive one stays in shell.json, so reading the raw
  //: setting would name a phrase nothing is listening for.
  readonly property string activePhrase:
    root.usingOww ? String(root.setting("owwModel", "hey_jarvis")).replace(/_/g, " ")
                  : String(root.setting("phrase", "hey computer"))
  readonly property string projectDir: setting("projectDir", "")

  //: Levels are stored per agent: trust is a judgement about one program's
  //: capabilities, and letting it survive `omarchy default agent` would hand
  //: the next one a decision nobody made about it.
  function setLevel(value) {
    var all = {}
    var current = cfg["permissions"]
    if (current && typeof current === "object")
      for (var k in current) all[k] = current[k]
    if (root.vAgent === "") return
    all[root.vAgent] = value
    persist("permissions", JSON.stringify(all), true)
  }

  function levelOption(value) {
    if (value === "trusted") return { label: "Trust everything here", value: "trusted" }
    if (value === "edits") return { label: "Edits here are fine, ask for commands", value: "edits" }
    return { label: root.agentLevels.indexOf("edits") >= 0
                    ? "Ask before every change" : "Read-only, never act unasked",
             value: "ask" }
  }
  readonly property var levelOptions: {
    var out = []
    for (var i = 0; i < root.agentLevels.length; i++)
      out.push(levelOption(String(root.agentLevels[i])))
    return out
  }
  readonly property bool speakReplies: setting("speakReplies", true)
  readonly property string engine: setting("engine", "vosk")
  readonly property bool usingOww: engine === "openwakeword"

  function open(payloadJson) {
    mode = "settings"
    try {
      var p = JSON.parse(payloadJson || "{}")
      if (p.mode === "permission") mode = "permission"
    } catch (e) {}

    opened = true
    if (mode === "permission") {
      pendingProbe.running = true
    } else {
      cfgReload.running = true
      scanCustom.running = true
      scanVoices.running = true
      stateFile.reload()
    }
  }

  function answer(verdict) {
    if (!request || !request.id) { dismiss(); return }
    permit.command = ["agentvoice", "permit", String(request.id), verdict]
    permit.running = true
    dismiss()
  }

  Process { id: permit }

  // The hook writes the request file and only tells us its id; read the rest
  // here so the two do not have to agree on a payload schema.
  Process {
    id: pendingProbe
    command: ["agentvoice", "pending"]
    stdout: StdioCollector {
      onStreamFinished: {
        try { root.request = JSON.parse(String(text).trim() || "{}") }
        catch (e) { root.request = ({}) }
        if (root.request && root.request.timeout)
          root.secondsLeft = Math.round(root.request.timeout)
      }
    }
  }

  // Silence is a denial, so the countdown is the honest thing to show.
  Timer {
    interval: 1000
    running: root.opened && root.mode === "permission"
    repeat: true
    onTriggered: {
      root.secondsLeft = Math.max(0, root.secondsLeft - 1)
      if (root.secondsLeft <= 0) root.dismiss()
    }
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

  // shell.json changes from more than one place: this screen, the panel's own
  // switch, `omarchy bar set`, and a refresh. This screen only re-read it when
  // it was opened or when it wrote, so anything changed underneath an open
  // screen stayed invisible here while the panel -- which the bar updates live
  // -- showed the new value. The two then disagreed, which is exactly the kind
  // of thing a settings screen must never do about permissions.
  FileView {
    id: cfgFile
    path: (Quickshell.env("XDG_CONFIG_HOME")
           || ((Quickshell.env("HOME") || "") + "/.config")) + "/omarchy/shell.json"
    watchChanges: true
    printErrors: false
    onFileChanged: {
      cfgFile.reload()
      cfgReload.running = true
    }
  }

  // shell.json is the store; jq pulls out just this widget's entry.
  Process {
    id: cfgReload
    command: ["bash", "-c",
      "jq -c '[.bar.layout[]?[]? | select(.id==\"duaneoca.agentvoice\")][0] // {}' " +
      "\"${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json\" 2>/dev/null || echo '{}'"]
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

  // Same recovery as the panel: a first install has no state file to watch,
  // so the watch never attaches and the screen never updates. A sibling of
  // the FileView, not a child -- its default property takes an adapter.
  Timer {
    interval: 2000
    running: root.opened && root.vState === "off"
    repeat: true
    onTriggered: stateFile.reload()
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
        if (d.agent !== undefined) root.vAgent = String(d.agent)
        if (d.level !== undefined) root.permissionLevel = String(d.level)
        if (d.posture !== undefined) root.vPosture = String(d.posture)
        if (d.levels !== undefined) root.agentLevels = d.levels
        if (d.remembers !== undefined) root.vRemembers = (d.remembers === true)
        if (d.overriding !== undefined) root.vOverriding = String(d.overriding)
        if (d.governed !== undefined) root.vGoverned = (d.governed === true)
        if (d.verifier !== undefined) root.vVerifier = (d.verifier === true)
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
      height: root.mode === "permission"
              ? Math.min(Style.space(300), parent.height - Style.gapsOut * 2)
              : Math.min(Style.space(660), parent.height - Style.gapsOut * 2)
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
            text: root.mode === "permission" ? "Allow this?" : "Agent Voice"
            color: root.mode === "permission" ? Color.urgent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.title
            font.bold: true
            width: parent.width - hint.width
          }
          Text {
            id: hint
            text: root.mode === "permission"
                  ? "denied in " + root.secondsLeft + "s"
                  : "esc to close"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        PanelSeparator { width: parent.width; foreground: root.foreground }

        // --- permission prompt ----------------------------------------
        Column {
          width: parent.width
          spacing: Style.spacing.md
          visible: root.mode === "permission"

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "The agent wants to run " + (root.request.tool || "a tool") + "."
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
          }

          // The command itself, verbatim. Paraphrasing what is about to run
          // would defeat the point of asking.
          BorderSurface {
            width: parent.width
            height: Math.max(Style.space(44), cmd.implicitHeight + Style.space(16))
            radius: Style.cornerRadius
            color: Qt.darker(root.background, 1.25)
            borderSpec: root.borderSpec
            Text {
              id: cmd
              anchors.fill: parent
              anchors.margins: Style.space(8)
              wrapMode: Text.WrapAnywhere
              text: root.request.summary || "(no detail)"
              color: root.foreground
              font.family: "monospace"
              font.pixelSize: Style.font.caption
            }
          }

          Row {
            width: parent.width
            spacing: Style.spacing.md
            Button {
              text: "Deny"
              fontFamily: root.fontFamily
              onClicked: root.answer("deny")
            }
            Button {
              text: "Allow once"
              fontFamily: root.fontFamily
              onClicked: root.answer("allow")
            }
          }

          Text {
            width: parent.width
            wrapMode: Text.WordWrap
            text: "Doing nothing denies it. Read-only tools are never asked about."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }
        }

        Flickable {
          id: formScroll
          visible: root.mode === "settings"
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

            // The four controls anyone actually reaches for. Everything below
            // is calibration you set once and forget.
            Toggle {
              width: parent.width
              label: "Speak replies aloud"
              description: "Off leaves the reply as text in the panel."
              checked: root.speakReplies
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.persist("speakReplies",
                                      root.speakReplies ? "false" : "true", true)
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

            // Left on rather than switched off for a backend that forgets:
            // it still saves the wake word, which is half of what it is for.
            // What it cannot do is follow on, and that failure is silent --
            // the window opens, it listens, it answers from nothing.
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: root.conversation && !root.vRemembers
              text: (root.vAgent === "" ? "This agent" : root.vAgent) +
                    " starts fresh on every turn, so this saves you the wake " +
                    "word and nothing more. Asking \"what did you mean by " +
                    "that?\" will get an answer to a question it has never seen."
              color: Color.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
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

            // The directory and what is allowed in it are one decision, not
            // two: a permission level with no root to apply to means nothing,
            // and changing where the agent works without revisiting what it
            // may do there is how you end up trusting the wrong folder.
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: !root.vGoverned
              text: "⚠  Not in use right now. The endpoint answering works on " +
                    "its own machine, in a directory it chooses — these two " +
                    "settings govern an agent started here, and reach nothing " +
                    "over there. Restrain that one where it runs."
              color: Color.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              width: parent.width
              text: "PROJECT"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            TextField {
              width: parent.width
              text: root.projectDir
              placeholderText: "~  (your home directory)"
              foreground: root.foreground
              font.family: root.fontFamily
              onEditingFinished: {
                if (text !== root.projectDir) root.persist("projectDir", text, false)
              }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "Where the agent works. Everything it reads, writes or runs " +
                    "happens here. Changing it starts a new conversation, because " +
                    "the agent keeps its history per project."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Dropdown {
              width: parent.width
              showLabel: true
              label: root.vAgent === "" ? "Permission level"
                                        : "Permission level for " + root.vAgent
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: root.levelOptions
              value: root.permissionLevel
              onChanged: function(v) { root.setLevel(v) }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: (root.vPosture !== "" ? "In force: " + root.vPosture + ".  " : "")
                    + (root.agentLevels.indexOf("edits") < 0
                       ? (root.vAgent === "" ? "" : root.vAgent + " cannot put a " +
                          "question on screen, so it is held read-only instead and " +
                          "may refuse work rather than ask. Only the trusted level " +
                          "changes that.")
                       : root.permissionLevel === "edits"
                       ? "Files inside the project can be edited without asking. " +
                         "Commands, web fetches and anything outside it still prompt."
                       : root.permissionLevel === "trusted"
                       ? "Nothing is asked. A spoken sentence can edit files and run " +
                         "commands here with nothing able to stop it."
                       : "Every change is prompted. Read-only tools are never asked " +
                         "about, and an unanswered prompt is denied.")
              color: root.permissionLevel === "trusted" ? Color.urgent : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: root.vOverriding !== ""
              text: "⚠  This endpoint is answering instead of " + root.vOverriding +
                    ", which is what Omarchy's own settings are set to. Clear " +
                    "both boxes below to go back to it."
              color: Color.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            PanelSeparator { width: parent.width; foreground: root.foreground }

            // --- talking and interrupting -----------------------------------
            // Not settings: the keys. They are here because the feature was
            // built, bound and still undiscoverable -- the first question
            // asked of it was how to make it stop.
            PanelSectionHeader {
              text: "TALKING TO IT"; foreground: root.dim; fontFamily: root.fontFamily
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              textFormat: Text.StyledText
              text: "<b>Hold F8</b> and speak, release when you are done — no " +
                    "wake word needed.<br>" +
                    "<b>Press F8</b> while it is replying to stop it. So does " +
                    "<b>Super+Ctrl+Space</b>, or the stop button in the panel.<br>" +
                    "<b>Super+Alt+Space</b> releases the microphone entirely."
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Toggle {
              width: parent.width
              label: "Let me interrupt by talking"
              description: "Stop speaking when it hears you over it. Measure " +
                           "first with: agentvoice calibrate"
              checked: root.setting("bargeIn", false) === true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.persist("bargeIn",
                  root.setting("bargeIn", false) === true ? "false" : "true", true)
            }

            KnobRow {
              width: parent.width
              visible: root.setting("bargeIn", false) === true
              label: "How much louder you must be"
              unit: "%"
              description: "Above its own voice, before it stops. Higher is " +
                           "harder to trigger by accident and on purpose."
              value: root.setting("bargeFactor", 150)
              minimum: 110; maximum: 400; stepSize: 10
              onCommitted: function(v) { root.persist("bargeFactor", v, true) }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "No spoken phrase can interrupt a reply: the microphone is " +
                    "ignored while it talks, and at a normal volume its own " +
                    "voice reaches the mic as loudly as yours anyway. " +
                    "Saying \"stop\" or \"never mind\" cancels a turn it has " +
                    "not taken yet — useful when the wake word fires by " +
                    "mistake, so nothing is sent to the agent."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            PanelSeparator { width: parent.width; foreground: root.foreground }

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

            // Whether one is in force was known only to a log line. Training
            // one and seeing the screen unchanged reads as the training
            // having failed.
            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              visible: root.usingOww
              text: root.vVerifier
                    ? "\u2713  In use for “" + root.activePhrase + "”. Only your " +
                      "voice saying it gets through."
                    : "No verifier for “" + root.activePhrase + "” yet — anyone " +
                      "saying the phrase can wake it. Verifiers are per phrase, " +
                      "so training a new wake word means training a new one."
              color: root.vVerifier ? root.foreground : root.dim
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
                    "one in a Colab notebook — about 2.5 hours on the free tier, " +
                    "90 minutes on Colab Pro. You edit two lines: the phrase and " +
                    "the output name.\n\n" +
                    "Only the small classifier head is trained; the feature models " +
                    "it sits on ship with openWakeWord and do not change. That is " +
                    "why it is quick, and why training is network-bound rather " +
                    "than GPU-bound."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Button {
              text: "Open the training notebook…"
              fontFamily: root.fontFamily
              onClicked: Quickshell.execDetached(["omarchy-launch-browser",
                "https://github.com/alfiedennen/openwakeword-colab-2026"])
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "Upstream's own notebook has not been maintained since 2023; " +
                    "this one is patched for current Python and torchaudio.\n\n" +
                    "Save the .onnx it gives you into " +
                    "~/.local/share/agentvoice/wakewords/ and it appears in the " +
                    "phrase list above, ready for its own verifier. Check it with " +
                    "'agentvoice monitor' before trusting it: this ships " +
                    "openwakeword 0.4.0, and a model built against a newer one " +
                    "should load but has not been proven to."
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

            KnobRow {
              width: parent.width
              scrollTarget: formScroll
              foreground: root.foreground; fontFamily: root.fontFamily
              label: "Hard ceiling on one turn"
              description: "A microphone stuck open stops here rather than " +
                           "recording forever."
              value: root.setting("maxUtteranceMs", 30000)
              minimum: 5000; maximum: 120000; stepSize: 5000
              onCommitted: function(v) { root.persist("maxUtteranceMs", v, true) }
            }



            // --- speech ---------------------------------------------------
            PanelSeparator { width: parent.width; foreground: root.foreground }

            Text {
              width: parent.width
              text: "ENDPOINT (optional)"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            TextField {
              width: parent.width
              text: root.setting("endpointUrl", "")
              placeholderText: "http://your-machine:11434/v1"
              foreground: root.foreground
              font.family: root.fontFamily
              onEditingFinished: {
                if (text !== root.setting("endpointUrl", ""))
                  root.persist("endpointUrl", text, false)
              }
            }

            TextField {
              width: parent.width
              text: root.setting("endpointModel", "")
              placeholderText: "model name, e.g. llama3.2"
              foreground: root.foreground
              font.family: root.fontFamily
              onEditingFinished: {
                if (text !== root.setting("endpointModel", ""))
                  root.persist("endpointModel", text, false)
              }
            }

            Toggle {
              width: parent.width
              visible: root.setting("endpointUrl", "") !== ""
              label: "This endpoint is an agent"
              description: "Tick when it can run commands or change files on " +
                           "its own host, as Hermes can. agentvoice cannot tell " +
                           "from the URL, so it claims nothing about the far " +
                           "end unless you say — and gives it longer to answer, " +
                           "because an agent goes quiet while it runs tools."
              checked: root.setting("endpointIsAgent", false) === true
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.persist("endpointIsAgent",
                  root.setting("endpointIsAgent", false) === true
                    ? "false" : "true", true)
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "Any OpenAI-compatible endpoint — Ollama, LM Studio, vLLM, " +
                    "OpenAI, xAI, Hermes. Set both boxes and it answers instead " +
                    "of the desktop's agent.\n" +
                    "agentvoice offers it no tools and gates nothing, so the " +
                    "permission level above does not reach it. What the endpoint " +
                    "itself can do is not visible from here: Ollama cannot touch " +
                    "anything, while Hermes has a terminal and a filesystem on " +
                    "its host. That is what the tickbox is for.\n" +
                    "Ollama and LM Studio need no key. For one that does, the " +
                    "login keyring is the best place — it unlocks when you log " +
                    "in and only this session can read it:\n" +
                    "  secret-tool store --label=agentvoice \\\n" +
                    "      service agentvoice endpoint api.openai.com\n" +
                    "Keyed by host and port, so two services on one machine do " +
                    "not share a key. Failing that, " +
                    "~/.config/agentvoice/endpoint.key. Never in this settings " +
                    "file — it is the desktop's config and gets copied around."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
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
              label: "Live transcript while you speak"
              fontFamily: root.fontFamily
              foreground: root.foreground
              options: [
                { label: "auto — only when it is free", value: "auto" },
                { label: "on — costs 114MB",            value: "on" },
                { label: "off",                          value: "off" }
              ]
              value: root.setting("livePartials", "auto")
              onChanged: function(v) { root.persist("livePartials", v, false) }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "Rough text shown as you talk, before Whisper returns. It " +
                    "needs the Vosk model resident. On the Vosk wake engine " +
                    "that model is already loaded, so it is free; on " +
                    "openWakeWord it is an extra 114MB."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
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

            PanelSeparator { width: parent.width; foreground: root.foreground }

            // Last on the page, and the only red control on it. Removal lives
            // inside the settings it removes because nothing runs on the way
            // out: `omarchy plugin remove` is an rm -rf with no hook, so the
            // only moment this can clean up after itself is while it exists.
            Button {
              text: "Remove Agent Voice…"
              fontFamily: root.fontFamily
              foreground: Color.urgent
              accent: Color.urgent
              onClicked: {
                // No nested `bash -c` here. The launcher already does
                // `cmd="$*"` and re-wraps it, and a second one swallowed the
                // flag as $0 -- which ran this as a fresh *install* instead.
                remover.command = ["omarchy-launch-floating-terminal-with-presentation",
                                   "\"$HOME/.local/share/agentvoice/app/install.sh\" " +
                                   "--uninstall && omarchy plugin remove " +
                                   "duaneoca.agentvoice --yes"]
                remover.running = true
                root.dismiss()
              }
            }

            Text {
              width: parent.width
              wrapMode: Text.WordWrap
              text: "Removes the engine, the service and the widget in one go. " +
                    "Your trained verifiers, recordings and API keys are kept, " +
                    "and it says where they are on the way out."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Process { id: remover }

            Item { width: 1; height: Style.space(12) }
          }
        }
      }
    }
  }
}
