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
  // the list instead of changing the setting. Optional: when it is not set,
  // the enclosing Flickable is found by walking up. Nine of ten callers passed
  // it and the tenth did not, and the one that did not swallowed the wheel
  // outright -- the pointer had only to cross that row for scrolling to stop.
  // A default nobody has to remember cannot be forgotten.
  property Flickable scrollTarget: null

  readonly property Flickable effectiveScrollTarget: scrollTarget || _findFlickable()

  function _findFlickable() {
    var p = knob.parent
    while (p) {
      if (p instanceof Flickable) return p
      p = p.parent
    }
    return null
  }

  signal committed(real value)

  property real shown: value
  onValueChanged: shown = value

  // PanelSlider rounds to an integer when `integer` is set but does not snap
  // to `step` -- that only governs its wheel handling -- so dragging produced
  // values like 4035ms against a 500ms step. Quantise here, for the label and
  // for what gets written.
  function snap(v) {
    var n = Math.round((v - knob.minimum) / knob.stepSize)
    return Math.max(knob.minimum,
                    Math.min(knob.maximum, knob.minimum + n * knob.stepSize))
  }
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
    onMoved: function(v) { knob.shown = knob.snap(v) }
    onReleased: function(v) {
      var snapped = knob.snap(v)
      knob.shown = snapped
      // Writing the snapped value feeds back through `value`, so the handle
      // settles on the notch rather than wherever the pointer stopped.
      knob.committed(snapped)
    }

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
        var f = knob.effectiveScrollTarget
        // Accept only what can be acted on. Accepting first and returning on a
        // missing target consumed the event and scrolled nothing, which is how
        // this was reported: not a slider that moved when it should not, but a
        // page that stopped moving.
        if (!f) {
          wheel.accepted = false
          return
        }
        wheel.accepted = true
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
