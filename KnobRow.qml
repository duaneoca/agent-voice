import QtQuick
import qs.Commons
import qs.Ui

// A labelled slider that reports its value with a unit and only commits on
// release, so dragging does not fire `omarchy bar set` on every frame.
// Shared by the bar panel and the settings overlay.
Column {
  id: knob

  property QtObject bar: null
  property color foreground: Color.foreground
  property color dim: Qt.darker(foreground, 1.55)
  property string fontFamily: Style.font.family

  property string label: ""
  property string description: ""
  property real value: 0
  property real minimum: 0
  property real maximum: 1
  property real stepSize: 1
  property string unit: "ms"

  // The Flickable this row lives in, so a wheel event over the slider scrolls
  // the list instead of changing the setting.
  property Flickable scrollTarget: null

  signal committed(real value)

  property real shown: value
  onValueChanged: shown = value
  spacing: Style.space(2)

  Row {
    width: parent.width
    Text {
      text: knob.label
      color: knob.foreground
      font.family: knob.fontFamily
      font.pixelSize: Style.font.body
      width: parent.width - valueText.width
      elide: Text.ElideRight
    }
    Text {
      id: valueText
      text: Math.round(knob.shown) + knob.unit
      color: knob.dim
      font.family: knob.fontFamily
      font.pixelSize: Style.font.body
    }
  }

  PanelSlider {
    id: slider
    width: parent.width
    bar: knob.bar
    minimum: knob.minimum
    maximum: knob.maximum
    step: knob.stepSize
    integer: true
    value: knob.value
    onMoved: function(v) { knob.shown = v }
    onReleased: function(v) { knob.shown = v; knob.committed(Math.round(v)) }

    // PanelSlider's own wheel handler steps the value and then fires
    // released(), which here means a write to shell.json -- so scrolling the
    // settings list past a slider silently changed the setting under the
    // pointer. This sits above that handler and turns the wheel back into
    // scrolling. Declared last so it stacks on top, and NoButton so press and
    // drag still reach the slider underneath.
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.NoButton
      onWheel: function(wheel) {
        wheel.accepted = true
        var f = knob.scrollTarget
        if (!f) return
        var limit = Math.max(0, f.contentHeight - f.height)
        f.contentY = Math.max(0, Math.min(limit, f.contentY - wheel.angleDelta.y))
      }
    }
  }

  Text {
    width: parent.width
    visible: knob.description !== ""
    wrapMode: Text.WordWrap
    text: knob.description
    color: knob.dim
    font.family: knob.fontFamily
    font.pixelSize: Style.font.caption
    bottomPadding: Style.space(4)
  }
}
