"""Validated object-space operations shared by commands and private previews."""

import copy
from collections.abc import Mapping
from dataclasses import replace
from uuid import UUID

from easypicture.domain.coordinates import validate_object_transform
from easypicture.domain.document import Document
from easypicture.domain.geometry import AffineTransform, Point
from easypicture.domain.layer import RasterObject
from easypicture.domain.spatial import object_bounds
from easypicture.domain.vector import Collection, DocumentNode, walk_nodes


def node_with_properties(node: DocumentNode, properties: Mapping[str, object]) -> DocumentNode:
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
