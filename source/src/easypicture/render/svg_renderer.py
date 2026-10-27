"""Qt rendering adapter for sanitized static SVG vector objects."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from threading import local

from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QPainter, QTransform
from PySide6.QtSvg import QSvgRenderer, QtSvg

from easypicture.domain import SvgObject
from easypicture.persistence.snapshots import SvgElementSnapshot
from easypicture.render.content_cache import ContentCache

type SvgLike = SvgObject | SvgElementSnapshot
_thread_caches = local()


def svg_content_cache() -> ContentCache[QSvgRenderer]:
    """QSvgRenderer belongs to its creating thread, including export workers."""
    cache: ContentCache[QSvgRenderer] | None = getattr(_thread_caches, "svg", None)
    if cache is None:
        cache = ContentCache(byte_budget=32 * 1024 * 1024, entry_budget=2048)
        _thread_caches.svg = cache
    return cache


def cached_svg_renderer(element: SvgLike) -> QSvgRenderer:
    description = (
        element.svg,
        element.fill_override,
        element.stroke_override,
        element.stroke_width_override,
    )
    return svg_content_cache().get(
        element.id,
        description,
        len(element.svg) * 8 + 4096,
        lambda: create_static_svg_renderer(svg_with_overrides(element).encode("utf-8")),
    )


def release_thread_svg_cache() -> None:
    cache = getattr(_thread_caches, "svg", None)
    if cache is not None:
        cache.clear()


def paint_svg_object(
    painter: QPainter,
    element: SvgLike,
    *,
    group_opacity: float = 1.0,
) -> None:
    if not element.visible or element.opacity <= 0.0 or group_opacity <= 0.0:
        return
    renderer = cached_svg_renderer(element)
    if not renderer.isValid():
        return
    matrix = element.transform
    painter.save()
    painter.setOpacity(painter.opacity() * element.opacity * group_opacity)
    painter.setWorldTransform(
        QTransform(matrix.a, matrix.b, matrix.c, matrix.d, matrix.tx, matrix.ty),
        True,
    )
    default_size = renderer.defaultSize()
    renderer.render(
        painter,
        QRectF(0.0, 0.0, float(default_size.width()), float(default_size.height())),
    )
    painter.restore()


def paint_svg_objects(
    painter: QPainter,
    objects: Iterable[SvgLike],
    *,
    group_opacity: float = 1.0,
) -> None:
    for element in objects:
        paint_svg_object(painter, element, group_opacity=group_opacity)


def create_static_svg_renderer(data: bytes) -> QSvgRenderer:
    renderer = QSvgRenderer()
    renderer.setOptions(QtSvg.Option.DisableAnimations)
    renderer.load(QByteArray(data))
    return renderer


def svg_with_overrides(element: SvgLike) -> str:
    if (
        element.fill_override is None
        and element.stroke_override is None
        and element.stroke_width_override is None
    ):
        return element.svg
    try:
        root = ET.fromstring(element.svg)
    except ET.ParseError:
        return element.svg
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for node in root.iter():
        kind = node.tag.rsplit("}", 1)[-1]
        if kind not in {"path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "use"}:
            continue
        ancestor = parent_map.get(node)
        if ancestor is not None and ancestor.tag.rsplit("}", 1)[-1] in {
            "defs",
            "clipPath",
        }:
            continue
        if element.fill_override is not None:
            node.set("fill", _svg_color(element.fill_override))
            node.set("fill-opacity", f"{element.fill_override[3] / 255.0:.12g}")
        if element.stroke_override is not None:
            node.set("stroke", _svg_color(element.stroke_override))
            node.set("stroke-opacity", f"{element.stroke_override[3] / 255.0:.12g}")
        if element.stroke_width_override is not None:
            node.set("stroke-width", f"{element.stroke_width_override:.12g}")
    return ET.tostring(root, encoding="unicode")


def _svg_color(color: tuple[int, int, int, int]) -> str:
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


__all__ = [
    "create_static_svg_renderer",
    "paint_svg_object",
    "paint_svg_objects",
    "svg_with_overrides",
]
