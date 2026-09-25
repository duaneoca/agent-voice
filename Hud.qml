import QtQuick
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// What it is doing, while it is doing it, without taking the keyboard.
//
// The obvious implementation was to call open() on the widget's own panel when
// the wake word fires. That panel is a KeyboardPanel, and its onOpenChanged
// sets focusPrimed = false and primes WlrKeyboardFocus.Exclusive -- so it takes
// keyboard focus on every open, and the flag cannot be pre-set because opening
// clears it. A reply plus its follow-up window is roughly 25 seconds of
// somebody's typing going to a status readout, which is the opposite of what a
// hands-free interface is for.
//
// So this is a layer-shell surface with keyboardFocus None and the exclusion
// zone ignored, the same shape Omarchy's own notifications use. It can never
// steal focus and never moves anything else on screen.
PanelWindow {
  id: hud

  //: Live state, passed in rather than re-read: the widget already watches the
  //: state file, and two watchers on one file would disagree during a write.
  property string vState: "off"
  property string stateLabel: ""
  property string icon: ""
  property string transcript: ""
  property color accent: Color.foreground

  //: `var`, not QtObject: the bar is reached for position and thickness, and
  //: a QtObject-typed handle makes those unresolvable to qmllint -- which
  //: the check script rejects, for the good reason that it cannot tell a
  //: real typo from a duck-typed one.
  property var bar: null
  property string fontFamily: Style.font.family
  property int lingerMs: 2000
  property bool enabled: true

  //: The states that mean an exchange is under way. `followup` is included:
  //: the window is still open, another sentence may follow, and closing on it
  //: would flicker between turns.
  readonly property bool busy: enabled
    && (vState === "capture" || vState === "thinking"
        || vState === "speaking" || vState === "followup")

  property bool showing: false

  onBusyChanged: {
    if (busy) {
      linger.stop()
      showing = true
    } else if (showing) {
      // Restart, not start: a rapid followup -> capture -> followup bounce
      // would otherwise leave an earlier timer to close it mid-sentence.
      linger.interval = Math.max(0, hud.lingerMs)
      linger.restart()
    }
  }

  onEnabledChanged: if (!enabled) { linger.stop(); showing = false }

  Timer {
    id: linger
    repeat: false
    onTriggered: hud.showing = false
  }

  visible: showing
  implicitWidth: Style.space(280)
  implicitHeight: body.implicitHeight + Style.space(12) * 2

  color: "transparent"
  exclusionMode: ExclusionMode.Ignore
  WlrLayershell.namespace: "agentvoice-hud"
  WlrLayershell.layer: WlrLayer.Overlay
  WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

  // Below a top bar, above a bottom one, and clear of a vertical one. Falls
  // back to a plain corner offset when the bar does not say where it is.
  readonly property string barSide: bar && bar.position ? bar.position : "top"
  anchors {
    top: hud.barSide !== "bottom"
    bottom: hud.barSide === "bottom"
    right: true
  }
  margins {
    top: hud.barSide === "top" ? hud.barThickness + Style.space(6) : Style.space(6)
    bottom: hud.barSide === "bottom" ? hud.barThickness + Style.space(6) : Style.space(6)
    right: hud.barSide === "right" ? hud.barThickness + Style.space(6) : Style.space(6)
  }
  readonly property int barThickness: bar && bar.height > 0 ? bar.height : Style.space(10)

  Rectangle {
    anchors.fill: parent
    radius: Style.cornerRadius
    color: Color.tooltip.background
    border.width: 1
    // Qt.rgba on the components, not Qt.alpha: the latter appears nowhere in
    // Omarchy's own QML, and an unverifiable helper in a surface nothing can
    // screenshot is a poor bet.
    border.color: Qt.rgba(hud.accent.r, hud.accent.g, hud.accent.b, 0.35)

    Column {
      id: body
      x: Style.space(12)
      y: Style.space(12)
      width: parent.width - Style.space(12) * 2
      spacing: Style.space(4)

      Row {
        spacing: Style.space(6)
        Text {
          text: hud.icon
          color: hud.accent
          font.family: hud.fontFamily
          font.pixelSize: Style.font.body
        }
        Text {
          text: hud.stateLabel
          color: Color.tooltip.text
          font.family: hud.fontFamily
          font.pixelSize: Style.font.caption
        }
      }

      Text {
        width: parent.width
        visible: hud.transcript !== ""
        wrapMode: Text.WordWrap
        maximumLineCount: 3
        elide: Text.ElideRight
        text: "“" + hud.transcript + "”"
        color: Color.tooltip.text
        font.family: hud.fontFamily
        font.pixelSize: Style.font.body
      }
    }
  }
}
