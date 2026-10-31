"""Shared paths for vector previews and affine raster output."""

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPolygonF,
    QTransform,
)

from easypicture.domain import (
    AffineTransform,
    RasterObject,
    SvgCircleGeometry,
    SvgEllipseGeometry,
    SvgLineGeometry,
    SvgObject,
    SvgPolygonGeometry,
    SvgRectGeometry,
    TextObject,
    parse_editable_svg,
)
from easypicture.imaging.shapes import ShapeGeometry, ShapeKind
from easypicture.pixels import DEFAULT_TILE_SIZE, TileCoord


def object_selection_path(node: RasterObject | TextObject | SvgObject) -> QPainterPath:
    """Creation geometry, without stroke/culling padding, mapped with the object."""
    path = QPainterPath()
    matrix = node.transform
    if isinstance(node, RasterObject):
        path.addRect(QRectF(0, 0, node.size.width, node.size.height))
        matrix = node.world_transform
    elif isinstance(node, TextObject):
        rect = node.frame
        path.addRect(QRectF(rect.x, rect.y, rect.width, rect.height))
    else:
        primitive = parse_editable_svg(node.svg)
        if primitive is None:
            rect = node.intrinsic_bounds
            path.addRect(QRectF(rect.x, rect.y, rect.width, rect.height))
        else:
            geometry = primitive.geometry
            if isinstance(geometry, SvgRectGeometry):
                shape_rect = QRectF(geometry.x, geometry.y, geometry.width, geometry.height)
                if geometry.rx > 0 and geometry.ry > 0:
                    path.addRoundedRect(shape_rect, geometry.rx, geometry.ry)
                else:
                    path.addRect(shape_rect)
            elif isinstance(geometry, (SvgCircleGeometry, SvgEllipseGeometry)):
                rx = geometry.r if isinstance(geometry, SvgCircleGeometry) else geometry.rx
                ry = geometry.r if isinstance(geometry, SvgCircleGeometry) else geometry.ry
                path.addEllipse(QRectF(geometry.cx - rx, geometry.cy - ry, rx * 2, ry * 2))
            elif isinstance(geometry, SvgLineGeometry):
                path.moveTo(geometry.x1, geometry.y1)
                path.lineTo(geometry.x2, geometry.y2)
            else:
                path.moveTo(*geometry.points[0])
                for point in geometry.points[1:]:
                    path.lineTo(*point)
                if isinstance(geometry, SvgPolygonGeometry):
                    path.closeSubpath()
    return QTransform(*matrix.values).map(path)


def shape_path(geometry: ShapeGeometry) -> QPainterPath:
    path = QPainterPath()
    if geometry.kind in (ShapeKind.LINE, ShapeKind.POLYLINE):
        path.moveTo(QPointF(*geometry.points[0]))
        for point in geometry.points[1:]:
            path.lineTo(QPointF(*point))
        return path
    if geometry.kind in (ShapeKind.POLYGON, ShapeKind.TRIANGLE):
        polygon = QPolygonF([QPointF(*point) for point in geometry.points])
        path.addPolygon(polygon)
        path.closeSubpath()
        return path
    left, top, right, bottom = geometry.bounds
    rect = QRectF(left, top, right - left, bottom - top)
    if geometry.kind is ShapeKind.ELLIPSE:
        path.addEllipse(rect)
    elif geometry.kind is ShapeKind.ROUNDED_RECTANGLE:
        radius = geometry.effective_corner_radius
        path.addRoundedRect(rect, radius, radius)
    else:
        path.addRect(rect)
    return path


def local_shape_paths(
    geometry: ShapeGeometry,
    width: float,
    inverse: AffineTransform,
) -> tuple[QPainterPath, QPainterPath]:
    path = shape_path(geometry)
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(
        Qt.PenJoinStyle.MiterJoin
        if geometry.kind is ShapeKind.RECTANGLE
        else Qt.PenJoinStyle.RoundJoin
    )
    matrix = QTransform(*inverse.values)
    return matrix.map(path), matrix.map(stroker.createStroke(path))


def path_coverage(path: QPainterPath, coord: TileCoord, antialias: bool) -> np.ndarray:
    image = QImage(DEFAULT_TILE_SIZE, DEFAULT_TILE_SIZE, QImage.Format.Format_Alpha8)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, antialias)
        painter.translate(-coord.x * DEFAULT_TILE_SIZE, -coord.y * DEFAULT_TILE_SIZE)
        painter.fillPath(path, QColor(255, 255, 255))
    finally:
        painter.end()
    return (
        np.frombuffer(image.constBits(), np.uint8)
        .reshape(DEFAULT_TILE_SIZE, image.bytesPerLine())[:, :DEFAULT_TILE_SIZE]
        .astype(np.float32)
        / 255
    )
