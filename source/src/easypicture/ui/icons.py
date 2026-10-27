"""Locally bundled Lucide icons, shared by tools and the object outliner."""

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from easypicture.domain import Collection, DocumentNode, RasterObject, SvgObject, TextObject

ICON_ROOT = Path(__file__).resolve().parents[1] / "resources" / "icons"
TOOL_ICONS = {
    "object": "mouse-pointer-2",
    "raster_object": "image-plus",
    "brush": "brush",
    "eraser": "eraser",
    "eyedropper": "pipette",
    "bucket": "paint-bucket",
    "rectangle_selection": "square-dashed",
    "magic_wand": "wand-sparkles",
    "crop": "crop",
    "transform": "move",
    "shape": "shapes",
    "text": "type",
    "sprite_slice": "scan-line",
}


@lru_cache(maxsize=128)
def icon(name: str, color: str = "#d8dee9") -> QIcon:
    data = (ICON_ROOT / f"{name}.svg").read_text(encoding="utf-8")
    renderer = QSvgRenderer(QByteArray(data.replace("currentColor", color).encode()))
    result = QIcon()
    for size in (16, 20, 24, 32, 48, 64):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(1, 1, size - 2, size - 2))
        painter.end()
        result.addPixmap(pixmap)
    return result


def object_icon(node: DocumentNode) -> QIcon:
    if isinstance(node, Collection):
        return icon("folder", "#d8b66b")
    if isinstance(node, RasterObject):
        return icon("image", "#75b9e6")
    if isinstance(node, TextObject):
        return icon("type", "#c4a2e8")
    name = {
        "rect": "square",
        "circle": "circle",
        "ellipse": "circle",
        "polygon": "pentagon",
        "polyline": "spline",
        "line": "spline",
    }.get(node.element_kind if isinstance(node, SvgObject) else "", "pen-tool")
    return icon(name, "#8ccf9d")
