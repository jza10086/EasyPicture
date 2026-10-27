"""Bounded uniform-grid index of conservative document-space object bounds."""

from __future__ import annotations

import math
from collections.abc import Iterable
from uuid import UUID

from easypicture.domain.coordinates import RasterMapping
from easypicture.domain.geometry import FloatRect, Point
from easypicture.domain.layer import RasterObject
from easypicture.domain.svg_primitives import parse_editable_svg
from easypicture.domain.vector import SvgObject, TextObject


def object_bounds(node: RasterObject | TextObject | SvgObject) -> FloatRect:
    if isinstance(node, RasterObject):
        return RasterMapping(node.world_transform, node.size).bounds
    rect = node.frame if isinstance(node, TextObject) else node.intrinsic_bounds
    # Imported SVG intrinsic bounds include its original stroke. Overrides can
    # extend it; conservatively allow the SVG default miter limit as well.
    padding = (
        max(0.0, node.stroke_width_override or 0.0) * 4 if isinstance(node, SvgObject) else 0.0
    )
    if isinstance(node, SvgObject):
        primitive = parse_editable_svg(node.svg)
        if primitive is not None and (
            primitive.style.stroke is not None or node.stroke_override is not None
        ):
            width = (
                primitive.style.stroke_width
                if node.stroke_width_override is None
                else node.stroke_width_override
            )
            padding = max(padding, width * max(1.0, primitive.style.miter_limit) / 2.0)
    points = tuple(
        node.transform.map_point(Point(x, y))
        for x in (rect.x - padding, rect.right + padding)
        for y in (rect.y - padding, rect.bottom + padding)
    )
    left, right = min(p.x for p in points), max(p.x for p in points)
    top, bottom = min(p.y for p in points), max(p.y for p in points)
    # A conservative AA fringe prevents viewport clipping of edge coverage.
    return FloatRect(left - 1, top - 1, right - left + 2, bottom - top + 2)


def bounds_description(node: RasterObject | TextObject | SvgObject) -> tuple[object, ...]:
    """Direct equality description; metadata/visibility never moves the bounds."""
    if isinstance(node, RasterObject):
        return node.size, node.origin, node.transform
    if isinstance(node, TextObject):
        return node.frame, node.transform
    return (
        node.svg,
        node.intrinsic_bounds,
        node.transform,
        node.stroke_override,
        node.stroke_width_override,
    )


def intersects(left: FloatRect, right: FloatRect) -> bool:
    return (
        left.x <= right.right
        and right.x <= left.right
        and left.y <= right.bottom
        and right.y <= left.bottom
    )


class ObjectBoundsIndex:
    """Large objects use a separate bucket to bound grid allocation per object."""

    def __init__(self, cell_size: int = 256, maximum_cells: int = 256) -> None:
        if cell_size <= 0 or maximum_cells <= 0:
            raise ValueError("spatial index limits must be positive")
        self.cell_size = cell_size
        self.maximum_cells = maximum_cells
        self._bounds: dict[UUID, FloatRect] = {}
        self._cells: dict[tuple[int, int], set[UUID]] = {}
        self._memberships: dict[UUID, tuple[tuple[int, int], ...]] = {}
        self._large: set[UUID] = set()

    def bounds(self, object_id: UUID) -> FloatRect | None:
        return self._bounds.get(object_id)

    @property
    def object_ids(self) -> frozenset[UUID]:
        return frozenset(self._bounds)

    def update(self, object_id: UUID, bounds: FloatRect) -> None:
        if self._bounds.get(object_id) == bounds:
            return
        self.remove((object_id,))
        self._bounds[object_id] = bounds
        cells = self._covered_cells(bounds)
        if cells is None:
            self._large.add(object_id)
        else:
            self._memberships[object_id] = cells
            for cell in cells:
                self._cells.setdefault(cell, set()).add(object_id)

    def remove(self, object_ids: Iterable[UUID]) -> None:
        for object_id in object_ids:
            self._bounds.pop(object_id, None)
            self._large.discard(object_id)
            for cell in self._memberships.pop(object_id, ()):
                members = self._cells[cell]
                members.discard(object_id)
                if not members:
                    del self._cells[cell]

    def query(self, bounds: FloatRect) -> frozenset[UUID]:
        cells = self._covered_cells(bounds)
        if cells is None:
            candidates = set(self._bounds)
        else:
            candidates = self._large.copy()
            for cell in cells:
                candidates.update(self._cells.get(cell, ()))
        return frozenset(
            object_id for object_id in candidates if intersects(self._bounds[object_id], bounds)
        )

    def at(self, point: Point) -> frozenset[UUID]:
        cell = math.floor(point.x / self.cell_size), math.floor(point.y / self.cell_size)
        candidates = self._large | self._cells.get(cell, set())
        return frozenset(
            object_id
            for object_id in candidates
            if (bounds := self._bounds[object_id]).x <= point.x <= bounds.right
            and bounds.y <= point.y <= bounds.bottom
        )

    def _covered_cells(self, bounds: FloatRect) -> tuple[tuple[int, int], ...] | None:
        x0, y0 = math.floor(bounds.x / self.cell_size), math.floor(bounds.y / self.cell_size)
        x1 = math.floor(bounds.right / self.cell_size)
        y1 = math.floor(bounds.bottom / self.cell_size)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > self.maximum_cells:
            return None
        return tuple((x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1))
