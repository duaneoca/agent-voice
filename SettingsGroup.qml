import QtQuick
import qs.Commons
import qs.Ui

// A titled box around settings that belong together.
//
// The page had grown to ten sections separated by horizontal rules, and the
// complaint was the obvious one: it is easy to get lost in it. A rule says
// "something changed here"; a box says "these belong to each other", which is
// the thing a reader needs when a knob only makes sense next to the switch
// above it -- the follow-up window without "keep listening" is a number
// governing nothing.
//
// Usage: children go straight inside, and size themselves to `parent.width`
// exactly as they did when they sat in the page's own Column.
//
//   SettingsGroup {
//     width: parent.width
//     title: "CONVERSATION"
//     Toggle { width: parent.width; ... }
//   }
Column {
  id: group

  property string title: ""
  property color foreground: Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.4)
  property string fontFamily: Style.font.family
  //: Outer padding inside the box. Named because the height below depends on
  //: it twice and a mismatch clips the last control.
  readonly property int pad: Style.space(10)

  //: Children land in the inner Column rather than here, so the box wraps
  //: them. Without `default` every caller would have to name a slot.
  default property alias items: inner.data

  spacing: Style.space(4)

  PanelSectionHeader {
    text: group.title
    visible: group.title !== ""
    foreground: group.foreground
    fontFamily: group.fontFamily
  }

  Rectangle {
    width: parent.width
    implicitHeight: inner.implicitHeight + group.pad * 2
    height: implicitHeight
    radius: Style.cornerRadius
    color: "transparent"
    // Dim rather than accented: a box per group, all accented, would be
    // louder than the controls inside them.
    border.width: 1
    border.color: Qt.rgba(group.foreground.r, group.foreground.g,
                          group.foreground.b, 0.18)

    Column {
      id: inner
      x: group.pad
      y: group.pad
      width: parent.width - group.pad * 2
      spacing: Style.space(6)
    }
  }
}
