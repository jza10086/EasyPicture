"""Validated object-space operations shared by commands and private previews."""

import copy
import math
from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from easypicture.domain.coordinates import validate_object_transform
from easypicture.domain.document import Document
from easypicture.domain.geometry import AffineTransform, FloatRect, Point
from easypicture.domain.layer import RasterObject
from easypicture.domain.spatial import object_bounds
from easypicture.domain.svg_primitives import (
    SvgPrimitiveStyle,
    parse_editable_svg,
    primitive_bounds,
)
from easypicture.domain.vector import Collection, DocumentNode, SvgObject, TextObject, walk_nodes

type TransformableObject = RasterObject | TextObject | SvgObject


def object_local_bounds(node: TransformableObject) -> FloatRect:
    """Editable geometry bounds; paint/stroke padding is never an object property."""
    if isinstance(node, RasterObject):
        return FloatRect(0, 0, node.size.width, node.size.height)
    if isinstance(node, TextObject):
        return node.frame
    primitive = parse_editable_svg(node.svg)
    return (
        primitive_bounds(primitive.geometry, SvgPrimitiveStyle(stroke=None))
        if primitive is not None
        else node.intrinsic_bounds
    )


def object_world_transform(node: TransformableObject) -> AffineTransform:
    return node.world_transform if isinstance(node, RasterObject) else node.transform


def object_pivot(node: TransformableObject) -> Point:
    if node.transform_pivot is not None:
        return node.transform_pivot
    bounds = object_local_bounds(node)
    return object_world_transform(node).map_point(
        Point(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)
    )


def object_center_offset(node: TransformableObject) -> Point:
    if node.transform_pivot is None:
        return Point(0, 0)
    bounds = object_local_bounds(node)
    local = object_world_transform(node).inverse().map_point(object_pivot(node))
    return Point(local.x - bounds.x - bounds.width / 2, local.y - bounds.y - bounds.height / 2)


def object_basis(node: TransformableObject) -> AffineTransform:
    """Unit local axes, retaining reflection and shear, without position or scale."""
    matrix = object_world_transform(node)
    x, y = math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)
    return AffineTransform(matrix.a / x, matrix.b / x, matrix.c / y, matrix.d / y)


def edited_object_transform(
    node: TransformableObject,
    position: Point,
    width: float,
    height: float,
    rotation: float,
    center_offset: Point,
) -> tuple[AffineTransform, Point | None]:
    """Absolute world origin, local-axis dimensions and local center offset."""
    bounds = object_local_bounds(node)
    old = object_world_transform(node)
    pivot = object_pivot(node)
    sx = width / (bounds.width * math.hypot(old.a, old.b))
    sy = height / (bounds.height * math.hypot(old.c, old.d))
    scaled = AffineTransform(old.a * sx, old.b * sx, old.c * sy, old.d * sy, old.tx, old.ty)
    delta_angle = rotation - math.degrees(math.atan2(old.b, old.a))
    # Scale columns in object space, then rotate in document space. Retain shear/reflection.
    local_pivot = old.inverse().map_point(pivot)
    mapped = scaled.map_point(local_pivot)
    matrix = (
        scaled.then(AffineTransform.translation(-mapped.x, -mapped.y))
        .then(AffineTransform.rotation(delta_angle))
        .then(AffineTransform.translation(position.x, position.y))
    )
    if sx == 1 and sy == 1 and delta_angle == 0:
        matrix = old.then(AffineTransform.translation(position.x - pivot.x, position.y - pivot.y))
    new_pivot = matrix.map_point(
        Point(
            bounds.x + bounds.width / 2 + center_offset.x,
            bounds.y + bounds.height / 2 + center_offset.y,
        )
    )
    if center_offset == object_center_offset(node):
        new_pivot = position
    if isinstance(node, RasterObject):
        matrix = matrix.then(AffineTransform.translation(-node.origin.x, -node.origin.y))
    validate_object_transform(matrix)
    auto_center = node.transform_pivot is None and center_offset == Point(0, 0)
    return matrix, None if auto_center else new_pivot


def node_with_properties[T: DocumentNode](node: T, properties: Mapping[str, object]) -> T:
    allowed = {
        "name",
        "visible",
        "opacity",
        "locked",
        "transform",
        "transform_pivot",
        "alpha_locked",
        "sampling",
    }
    if set(properties) - allowed:
        raise ValueError("unknown object property")
    candidate = copy.copy(node)
    for name, value in properties.items():
        if not hasattr(node, name):
            raise ValueError("property is unavailable for this object type")
        if name == "name" and (
            not isinstance(value, str) or not value.strip() or len(value) > 1024
        ):
            raise ValueError("invalid object name")
        if name in {"visible", "locked", "alpha_locked"} and type(value) is not bool:
            raise ValueError("object flag must be boolean")
        if name == "opacity" and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1
        ):
            raise ValueError("invalid object opacity")
        if name == "transform":
            if not isinstance(value, AffineTransform):
                raise ValueError("object transform must be affine")
            validate_object_transform(value)
        if name == "transform_pivot" and value is not None and not isinstance(value, Point):
            raise ValueError("invalid object pivot")
        setattr(candidate, name, value)
    candidate.__post_init__()
    return candidate


def transformed_object_matrices(
    document: Document,
    ids: tuple[UUID, ...],
    delta: AffineTransform,
) -> dict[UUID, AffineTransform]:
    validate_object_transform(delta)
    result: dict[UUID, AffineTransform] = {}
    for key in dict.fromkeys(ids):
        for node in walk_nodes((document.node_by_id(key),)):
            if document.is_locked(node.id):
                raise PermissionError("选中目标包含锁定对象，整个操作已拒绝")
            if isinstance(node, Collection):
                continue
            matrix = node.transform.then(delta)
            if isinstance(node, RasterObject):
                matrix = node.world_transform.then(delta).then(
                    AffineTransform.translation(-node.origin.x, -node.origin.y)
                )
            validate_object_transform(matrix)
            object_bounds(replace(node, transform=matrix))
            result[node.id] = matrix
    return result
