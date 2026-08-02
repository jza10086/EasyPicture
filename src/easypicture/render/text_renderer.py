"""Qt text-layout adapter shared by canvas, thumbnails, and image export."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QTextLayout,
    QTextLine,
    QTextOption,
    QTransform,
)

from easypicture.domain import (
    SvgElementObject,
    TextHorizontalAlignment,
    TextObject,
    TextVerticalAlignment,
    TextWrapMode,
    VectorGroupLayer,
)
from easypicture.persistence.snapshots import SvgElementSnapshot

if TYPE_CHECKING:
    from easypicture.persistence.snapshots import (
        TextObjectSnapshot,
        VectorGroupSnapshot,
    )

type TextLike = TextObject | TextObjectSnapshot
type VectorLike = TextLike | SvgElementObject | SvgElementSnapshot
type GroupLike = VectorGroupLayer | VectorGroupSnapshot


def paint_text_object(
    painter: QPainter,
    text_object: TextLike,
    *,
    group_opacity: float = 1.0,
) -> None:
    """Paint one fixed-frame text object at document coordinates."""
    if not text_object.visible or text_object.opacity <= 0.0 or group_opacity <= 0.0:
        return
    frame = text_object.frame
    font = QFont(text_object.font_family)
    font.setPointSizeF(text_object.font_size)
    font.setWeight(QFont.Weight(text_object.font_weight))
    font.setItalic(text_object.italic)
    option = QTextOption()
    option.setWrapMode(
        QTextOption.WrapMode.WrapAnywhere
        if text_object.wrap_mode is TextWrapMode.AUTO
        else QTextOption.WrapMode.NoWrap
    )
    if text_object.horizontal_alignment is TextHorizontalAlignment.CENTER:
        option.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    elif text_object.horizontal_alignment is TextHorizontalAlignment.RIGHT:
        option.setAlignment(Qt.AlignmentFlag.AlignRight)
    else:
        option.setAlignment(Qt.AlignmentFlag.AlignLeft)

    layouts: list[tuple[QTextLayout, list[QTextLine], float, str]] = []
    cursor_y = 0.0
    line_spacing = max(0.25, text_object.line_spacing)
    empty_line_height = QFontMetricsF(font).height() * line_spacing
    for paragraph in text_object.text.split("\n"):
        layout = QTextLayout(paragraph, font)
        layout.setTextOption(option)
        layout.beginLayout()
        lines: list[QTextLine] = []
        paragraph_height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(frame.width)
            line.setPosition(QPointF(0.0, paragraph_height))
            lines.append(line)
            paragraph_height += line.height() * line_spacing
        layout.endLayout()
        layouts.append((layout, lines, cursor_y, paragraph))
        cursor_y += paragraph_height if lines else empty_line_height
    total_height = cursor_y
    if text_object.vertical_alignment is TextVerticalAlignment.CENTER:
        offset_y = max(0.0, (frame.height - total_height) / 2.0)
    elif text_object.vertical_alignment is TextVerticalAlignment.BOTTOM:
        offset_y = max(0.0, frame.height - total_height)
    else:
        offset_y = 0.0

    color = text_object.color
    painter.save()
    transform = QTransform(
        text_object.transform.a,
        text_object.transform.b,
        text_object.transform.c,
        text_object.transform.d,
        text_object.transform.tx,
        text_object.transform.ty,
    )
    painter.setWorldTransform(transform, combine=True)
    painter.setClipRect(QRectF(frame.x, frame.y, frame.width, frame.height))
    painter.setOpacity(
        max(0.0, min(1.0, float(text_object.opacity) * float(group_opacity)))
    )
    stroke_width = float(text_object.stroke_width)
    stroke_color = text_object.stroke_color
    if stroke_width > 0.0 and stroke_color[3] > 0:
        outline = QPainterPath()
        for _layout, lines, paragraph_y, paragraph in layouts:
            for line in lines:
                start = line.textStart()
                line_text = paragraph[start : start + line.textLength()]
                if not line_text:
                    continue
                natural_width = line.naturalTextWidth()
                if text_object.horizontal_alignment is TextHorizontalAlignment.CENTER:
                    line_x = max(0.0, (frame.width - natural_width) / 2.0)
                elif text_object.horizontal_alignment is TextHorizontalAlignment.RIGHT:
                    line_x = max(0.0, frame.width - natural_width)
                else:
                    line_x = 0.0
                outline.addText(
                    QPointF(
                        frame.x + line_x,
                        frame.y
                        + offset_y
                        + paragraph_y
                        + line.position().y()
                        + line.ascent(),
                    ),
                    font,
                    line_text,
                )
        stroke_pen = QPen(
            QColor(
                stroke_color[0],
                stroke_color[1],
                stroke_color[2],
                stroke_color[3],
            )
        )
        stroke_pen.setWidthF(stroke_width * 2.0)
        stroke_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        stroke_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(stroke_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(outline)
    painter.setPen(QColor(color[0], color[1], color[2], color[3]))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for layout, _lines, paragraph_y, _paragraph in layouts:
        layout.draw(painter, QPointF(frame.x, frame.y + offset_y + paragraph_y))
    painter.restore()


def paint_vector_groups(
    painter: QPainter,
    groups: Iterable[GroupLike],
) -> None:
    """Paint visible vector groups in their already ordered node sequence."""
    for group in groups:
        if not group.visible or group.opacity <= 0.0:
            continue
        paint_vector_objects(painter, group.children, group_opacity=group.opacity)


def paint_vector_objects(
    painter: QPainter,
    objects: Iterable[VectorLike],
    *,
    group_opacity: float = 1.0,
) -> None:
    from easypicture.render.svg_renderer import paint_svg_object

    for vector_object in objects:
        if isinstance(vector_object, (SvgElementObject, SvgElementSnapshot)):
            paint_svg_object(painter, vector_object, group_opacity=group_opacity)
        else:
            paint_text_object(painter, vector_object, group_opacity=group_opacity)


__all__ = ["paint_text_object", "paint_vector_groups", "paint_vector_objects"]
