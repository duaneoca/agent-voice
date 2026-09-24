import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Agent Voice bar widget: one icon, one panel, one switch.
//
// The icon's whole job is principle 3 -- you own the mic. Linux gives us no
// OS-level microphone gate, so a wake word means something is listening
// continuously and the only honest answer is to make that impossible to miss.
// Recording is drawn in the urgent colour; released is dimmed.
//
// The id is `voice`, not `root`. A Component assigned to one of PanelHero's
// properties is instantiated in PanelHero's context, where `root` resolves to
// PanelHero -- so anything reaching back into this file has to do it through a
// name PanelHero does not also use.
Panel {
  id: voice
  moduleName: "duaneoca.agentvoice"
  ipcTarget: "duaneoca.agentvoice"

  // The bar sizes each widget from its implicit size. Without these the slot
  // is allocated 0x0, the icon anchors to nothing, and the widget is simply
  // absent -- no warning, no error, nothing in the log.
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // --- Theme ------------------------------------------------------------------
  readonly property color fg: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(fg, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  // --- Daemon state -----------------------------------------------------------
  property bool serviceActive: false
  property bool busy: false
  property string vState: "off"
  property string lastTranscript: ""
  property int lastMs: 0
  property real lastAudioS: 0
  property real levelDb: -99
  property string lastReply: ""

  readonly property bool live: serviceActive && vState !== "off"

  readonly property string icon: {
    if (!serviceActive) return "󰍭"              // mic off
    if (vState === "capture" || vState === "followup") return "󰑊"  // recording you
    if (vState === "thinking") return "󰔟"
    if (vState === "speaking") return "󰕾"
    if (vState === "listening") return "󰍬"
    return "󰍭"
  }

  readonly property color barIconColor: {
    if ((vState === "capture" || vState === "followup") && serviceActive)
      return bar ? bar.urgent : Color.urgent
    if (live) return barForeground
    return Qt.darker(barForeground, 1.55)
  }

  readonly property string stateLabel: {
    if (!serviceActive) return "OFF"
    if (vState === "capture") return "LISTENING TO YOU"
    // Still hot after a reply, waiting to see if you carry on.
    if (vState === "followup") return "STILL LISTENING"
    if (vState === "thinking") return "TRANSCRIBING"
    if (vState === "speaking") return "SPEAKING"
    if (vState === "listening") return "WAITING FOR \"" + voice.activePhrase.toUpperCase() + "\""
    return "STARTING"
  }

  // --- Settings ---------------------------------------------------------------
  // Both engines keep their own phrase key, and the inactive one stays in
  // shell.json rather than being cleared -- so switching to openWakeWord left
  // the panel announcing the Vosk phrase it was no longer listening for.
  // Everything user-facing reads activePhrase, never the raw keys.
  readonly property string engine: setting("engine", "vosk")
  readonly property bool usingOww: engine === "openwakeword"
  readonly property string phrase: setting("phrase", "hey computer")
  readonly property string owwModel: setting("owwModel", "hey_jarvis")
  readonly property string activePhrase:
      usingOww ? owwModel.replace(/_/g, " ") : phrase
  readonly property int leadInMs: setting("leadInMs", 5000)
  readonly property int trailingMs: setting("trailingSilenceMs", 1200)
  readonly property int thresholdDb: setting("micThresholdDb", -38)
  readonly property int confidencePct: setting("wakeConfidencePct", 70)
  readonly property int echoTailMs: setting("echoTailMs", 350)
  readonly property string voiceName: setting("voice", "lessac-medium")
  readonly property string model: setting("model", "tiny.en")

  readonly property var phraseOptions: [
    "hey computer", "okay computer", "hey agent", "hey claude", "okay claude",
    "hey assistant", "hey jarvis", "hey machine", "computer"
  ]
  // Labelled, because a filename is not a description of a voice.
  readonly property var voiceOptions: [
    { label: "Lessac — female, medium", value: "lessac-medium" },
    { label: "Lessac — female, low",    value: "lessac-low" },
    { label: "Amy — female, medium",    value: "amy-medium" },
    { label: "Ryan — male, medium",     value: "ryan-medium" },
    { label: "Joe — male, medium",      value: "joe-medium" },
    { label: "HFC — male, medium",      value: "hfc_male-medium" }
  ]
  readonly property bool speakReplies: setting("speakReplies", true)

  function persist(key, value) {
    setter.command = ["omarchy", "bar", "set", "duaneoca.agentvoice",
                      key, String(value), "--json"]
    setter.running = true
  }

  // The switch runs the service; `mic` only releases the microphone while
  // leaving the daemon up, which is the cheap panic button on right-click.
  function toggleService() {
    voice.busy = true
    switcher.command = ["agentvoice", "toggle"]
    switcher.running = true
  }

  Process { id: setter }

  Process {
    id: switcher
    onExited: function(code) {
      voice.busy = false
      probe.running = true            // re-read the truth rather than assume
    }
  }

  Process { id: micToggle; command: ["agentvoice", "mic"] }
  Process { id: interrupt; command: ["agentvoice", "interrupt"] }

  // What the agent is pointed at, and how much it may do there. On the panel
  // rather than behind the gear because it is the thing to check *before*
  // speaking -- what am I about to change, and can it act without asking.
  readonly property string projectDir: voice.setting("projectDir", "")
  // Agent, level and posture come from the daemon's state file rather than
  // from shell.json: what a level *means* depends on what the running agent
  // can honour, and only the daemon knows that. Keeping a second copy here
  // is how a screen ends up promising a guarantee that is not in force.
  property string vAgent: ""
  property string permissionLevel: "ask"
  property string vPosture: ""
  // Shown because conversation mode fails quietly on a backend that does
  // not remember: the window opens, it listens, it answers, and every turn
  // starts from nothing.
  property bool vRemembers: true
  // Non-empty when an endpoint is configured and is answering instead of
  // the agent chosen in Omarchy's settings.
  property string vOverriding: ""
  readonly property string projectLabel: {
    var d = String(voice.projectDir).trim()
    if (d === "") return "~"
    var home = Quickshell.env("HOME") || ""
    if (home !== "" && d.indexOf(home) === 0) return "~" + d.slice(home.length)
    return d
  }
  readonly property color permissionColor:
    voice.permissionLevel === "trusted" ? voice.urgent : voice.dim
  readonly property string permissionLabel:
    (voice.vAgent === "" ? "" : voice.vAgent + " · ")
    + (voice.vPosture !== "" ? voice.vPosture : "asks before every change")
    + (voice.vRemembers ? "" : " · does not remember")
    + (voice.vOverriding === "" ? "" : " · instead of " + voice.vOverriding)

  // systemd is the authority on whether the daemon exists; the state file only
  // says what it is doing. Both are needed: a stale state file outlives a
  // crashed daemon, and would otherwise leave the switch stuck on.
  Process {
    id: probe
    command: ["agentvoice", "is-active"]
    stdout: StdioCollector {
      onStreamFinished: voice.serviceActive = String(text).trim() === "yes"
    }
  }

  Timer {
    interval: 2000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: if (!probe.running) probe.running = true
  }

  FileView {
    id: stateFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000") + "/agentvoice/state"
    watchChanges: true
    printErrors: false
    // text() is stale inside the change signal, so route every path through
    // reload -> onLoaded and always parse fresh content.
    onFileChanged: stateFile.reload()
    onLoadFailed: voice.vState = "off"
    onLoaded: {
      try {
        var d = JSON.parse(text())
        voice.vState = String(d.state || "off")
        if (d.transcript !== undefined) voice.lastTranscript = String(d.transcript)
        if (d.ms !== undefined) voice.lastMs = Math.round(d.ms)
        if (d.audio_s !== undefined) voice.lastAudioS = d.audio_s
        if (d.level_db !== undefined) voice.levelDb = d.level_db
        if (d.reply !== undefined) voice.lastReply = String(d.reply)
        if (d.agent !== undefined) voice.vAgent = String(d.agent)
        if (d.level !== undefined) voice.permissionLevel = String(d.level)
        if (d.posture !== undefined) voice.vPosture = String(d.posture)
        if (d.remembers !== undefined) voice.vRemembers = (d.remembers === true)
        if (d.overriding !== undefined) voice.vOverriding = String(d.overriding)
      } catch (e) {
        voice.vState = "off"
      }
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: voice.bar
    text: voice.icon
    fontFamily: voice.bar ? voice.bar.fontFamily : Style.font.family
    foreground: voice.barIconColor
    tooltipText: voice.opened ? "" : "Agent Voice — " + voice.stateLabel
    // WidgetButton emits pressed(buttonCode); there is no onClicked.
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) {
        if (voice.serviceActive) micToggle.running = true
        else voice.toggleService()
      } else if (buttonCode === Qt.MiddleButton) {
        voice.toggleService()
      } else {
        voice.toggle()
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: voice
    bar: voice.bar
    open: voice.opened
    contentWidth: Style.space(360)
    contentHeight: panel.fittedContentHeight(content.implicitHeight, Style.space(560))

    Column {
      id: content
      width: parent.width
      spacing: Style.spacing.md

      // The switch lives in the hero because turning the thing on is the first
      // question anyone has when they open this panel.
      PanelHero {
        id: hero
        width: parent.width
        title: "Agent Voice"
        meta: voice.stateLabel
        foreground: voice.fg
        fontFamily: voice.fontFamily
        iconOpacity: voice.serviceActive ? 1.0 : 0.5

        iconComponent: Component {
          Text {
            text: voice.icon
            color: voice.barIconColor
            font.family: voice.fontFamily
            font.pixelSize: Style.font.display
          }
        }

        trailingControl: Component {
          ToggleSwitch {
            checked: voice.serviceActive
            busy: voice.busy
            foreground: voice.fg
            onToggled: voice.toggleService()
          }
        }
      }

      // The MouseArea is the container rather than an overlay: a child with
      // anchors.fill inside a Column disables the Column outright, which is
      // how this block came to be written, shipped, and invisible.
      MouseArea {
        width: parent.width
        height: projectRows.implicitHeight
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: { settings.running = true; voice.close() }

        Column {
          id: projectRows
          width: parent.width
          spacing: Style.space(3)

          Row {
            width: parent.width
            spacing: Style.spacing.sm
            Text {
              text: "\uf07b"
              color: voice.dim
              font.family: voice.fontFamily
              font.pixelSize: Style.font.body
            }
            Text {
              width: parent.width - Style.space(22)
              elide: Text.ElideMiddle
              text: voice.projectLabel
              color: voice.fg
              font.family: voice.fontFamily
              font.pixelSize: Style.font.body
            }
          }

          Row {
            width: parent.width
            spacing: Style.spacing.sm
            Text {
              text: "\uf023"
              color: voice.permissionColor
              font.family: voice.fontFamily
              font.pixelSize: Style.font.caption
            }
            Text {
              text: voice.permissionLabel
              color: voice.permissionColor
              font.family: voice.fontFamily
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      // What it last heard. On screen, because the whole problem with an
      // interface you cannot see is being unsure whether it understood you.
      Column {
        width: parent.width
        spacing: Style.space(4)
        visible: voice.lastTranscript !== ""

        PanelSectionHeader {
          text: "LAST HEARD"
          foreground: voice.dim
          fontFamily: voice.fontFamily
        }
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          text: voice.lastTranscript
          color: voice.fg
          font.family: voice.fontFamily
          font.pixelSize: Style.font.body
        }
        Text {
          text: voice.lastAudioS.toFixed(1) + "s audio · whisper " + voice.lastMs + "ms"
          color: voice.dim
          font.family: voice.fontFamily
          font.pixelSize: Style.font.caption
        }

        // The agent's answer, so the panel is a transcript of the exchange
        // rather than only of your half of it.
        Text {
          width: parent.width
          wrapMode: Text.WordWrap
          visible: voice.lastReply !== ""
          topPadding: Style.space(6)
          text: "→ " + voice.lastReply
          color: voice.dim
          font.family: voice.fontFamily
          font.pixelSize: Style.font.body
        }
      }

      // Barge-in cannot work acoustically here -- the microphone sits beside
      // the speaker -- so stopping a reply is an explicit act. This is the
      // discoverable half of it; Super+Ctrl+Space is the fast half.
      Text {
        visible: voice.vState === "speaking" || voice.vState === "thinking"
        text: "\uf04d   stop"
        color: stopHover.containsMouse ? voice.fg : voice.dim
        font.family: voice.fontFamily
        font.pixelSize: Style.font.caption
        MouseArea {
          id: stopHover
          anchors.fill: parent
          anchors.margins: -Style.space(6)
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: interrupt.running = true
        }
      }

      PanelSeparator { width: parent.width; foreground: voice.fg }

      // The knobs used to live here. Nine of them in a popup anchored to a
      // 26px bar icon was unreadable, and they are settings you change once
      // rather than watch, so they moved to the overlay. What stays is what
      // you glance at: state, the last exchange, and the switch.
      Row {
        width: parent.width
        spacing: Style.spacing.md

        Text {
          text: "“" + voice.activePhrase + "” · "
                + (voice.usingOww ? "openWakeWord" : "vosk") + " · " + voice.model
                + (voice.speakReplies ? " · " + voice.voiceName : " · muted")
          color: voice.dim
          font.family: voice.fontFamily
          font.pixelSize: Style.font.caption
          width: parent.width - gear.width - Style.spacing.md
          elide: Text.ElideRight
        }

        Text {
          id: gear
          text: "\uf013   settings"
          color: gearHover.containsMouse ? voice.fg : voice.dim
          font.family: voice.fontFamily
          font.pixelSize: Style.font.caption
          MouseArea {
            id: gearHover
            anchors.fill: parent
            anchors.margins: -Style.space(6)
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: {
              settings.running = true
              voice.close()
            }
          }
        }
      }
    }
  }

  // The overlay is the same plugin, summoned by id.
  Process {
    id: settings
    command: ["omarchy-shell", "shell", "summon", "duaneoca.agentvoice", "{}"]
  }

}
