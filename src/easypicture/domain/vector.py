"""Qt-free vector layer and editable text object entities."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from easypicture.domain.geometry import AffineTransform, FloatRect, Point
from easypicture.domain.layer import RasterLayer

type RGBAColor = tuple[int, int, int, int]

MAX_TEXT_LENGTH = 1 * 1024 * 1024
MAX_FONT_FAMILY_LENGTH = 255
MAX_TEXT_FONT_SIZE = 1024.0
MAX_TEXT_STROKE_WIDTH = 128.0
MAX_SVG_FRAGMENT_BYTES = 16 * 1024 * 1024


class VectorObjectKind(StrEnum):
    SVG_ELEMENT = "svg_element"
    SVG_TEXT = "svg_text"


class LayerGroupKind(StrEnum):
    RASTER = "raster_group"
    VECTOR = "vector_group"


class TextHorizontalAlignment(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class TextVerticalAlignment(StrEnum):
    TOP = "top"
    CENTER = "center"
    BOTTOM = "bottom"


class TextWrapMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


def _validate_color(color: RGBAColor) -> None:
    if len(color) != 4 or any(not isinstance(channel, int) for channel in color):
        raise ValueError("text color must contain four integer channels")
    if any(channel < 0 or channel > 255 for channel in color):
        raise ValueError("text color channels must be between 0 and 255")


@dataclass(slots=True)
class TextObject:
    id: UUID
    name: str
    text: str
    frame: FloatRect
    font_family: str = "Segoe UI"
    font_size: float = 32.0
    font_weight: int = 400
    italic: bool = False
    color: RGBAColor = (255, 255, 255, 255)
    stroke_color: RGBAColor = (0, 0, 0, 255)
    stroke_width: float = 0.0
    horizontal_alignment: TextHorizontalAlignment = TextHorizontalAlignment.LEFT
    vertical_alignment: TextVerticalAlignment = TextVerticalAlignment.TOP
    wrap_mode: TextWrapMode = TextWrapMode.AUTO
    line_spacing: float = 1.0
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False
    transform: AffineTransform = field(default_factory=AffineTransform)
    transform_pivot: Point | None = None
    _revision: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("text object name must not be empty")
        if len(self.name) > 1024:
            raise ValueError("text object name is too long")
        if not isinstance(self.text, str) or len(self.text.encode("utf-8")) > MAX_TEXT_LENGTH:
            raise ValueError("text content is too large")
        self.font_family = self.font_family.strip()
        if not self.font_family or len(self.font_family) > MAX_FONT_FAMILY_LENGTH:
            raise ValueError("font family is invalid")
        if not math.isfinite(self.font_size) or not 1.0 <= self.font_size <= MAX_TEXT_FONT_SIZE:
            raise ValueError("text font size is out of range")
        if (
            not math.isfinite(self.stroke_width)
            or not 0.0 <= self.stroke_width <= MAX_TEXT_STROKE_WIDTH
        ):
            raise ValueError("text stroke width is out of range")
        if not 100 <= self.font_weight <= 900 or self.font_weight % 100:
            raise ValueError("text font weight is invalid")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("text opacity must be between 0 and 1")
        if not 0.25 <= self.line_spacing <= 8.0:
            raise ValueError("text line spacing is out of range")
        if not isinstance(self.wrap_mode, TextWrapMode):
            raise ValueError("text wrap mode is invalid")
        _validate_color(self.color)
        _validate_color(self.stroke_color)
        if self.transform.determinant == 0.0:
            raise ValueError("text transform must be invertible")

    @classmethod
    def create(
        cls,
        frame: FloatRect,
        *,
        name: str = "文本",
        text: str = "文本",
    ) -> TextObject:
        return cls(id=uuid4(), name=name, text=text, frame=frame)

    @property
    def revision(self) -> int:
        return self._revision

    def record_change(self) -> None:
        self._revision += 1

    def duplicate(self, name: str | None = None) -> TextObject:
        return TextObject(
            id=uuid4(),
            name=name if name is not None else f"{self.name} 副本",
            text=self.text,
            frame=self.frame,
            font_family=self.font_family,
            font_size=self.font_size,
            font_weight=self.font_weight,
            italic=self.italic,
            color=self.color,
            stroke_color=self.stroke_color,
            stroke_width=self.stroke_width,
            horizontal_alignment=self.horizontal_alignment,
            vertical_alignment=self.vertical_alignment,
            wrap_mode=self.wrap_mode,
            line_spacing=self.line_spacing,
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
            transform=self.transform,
            transform_pivot=self.transform_pivot,
        )


@dataclass(slots=True)
class SvgElementObject:
    """A sanitized, standalone static SVG leaf element."""

    id: UUID
    name: str
    element_kind: str
    svg: str
    intrinsic_bounds: FloatRect
    transform: AffineTransform = field(default_factory=AffineTransform)
    transform_pivot: Point | None = None
    fill_override: RGBAColor | None = None
    stroke_override: RGBAColor | None = None
    stroke_width_override: float | None = None
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False
    _revision: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name or len(self.name) > 1024:
            raise ValueError("SVG object name is invalid")
        self.element_kind = self.element_kind.strip().lower()
        if not self.element_kind or len(self.element_kind) > 64:
            raise ValueError("SVG element kind is invalid")
        if not isinstance(self.svg, str) or not self.svg.strip():
            raise ValueError("SVG element document must not be empty")
        if len(self.svg.encode("utf-8")) > MAX_SVG_FRAGMENT_BYTES:
            raise ValueError("SVG element document is too large")
        if self.intrinsic_bounds.width <= 0.0 or self.intrinsic_bounds.height <= 0.0:
            raise ValueError("SVG element bounds must be positive")
        if self.transform.determinant == 0.0:
            raise ValueError("SVG element transform must be invertible")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("SVG element opacity must be between 0 and 1")
        if self.fill_override is not None:
            _validate_color(self.fill_override)
        if self.stroke_override is not None:
            _validate_color(self.stroke_override)
        if self.stroke_width_override is not None and (
            not math.isfinite(self.stroke_width_override)
            or not 0.0 <= self.stroke_width_override <= 8192.0
        ):
            raise ValueError("SVG stroke width override is out of range")

    @property
    def revision(self) -> int:
        return self._revision

    def record_change(self) -> None:
        self._revision += 1

    def duplicate(self, name: str | None = None) -> SvgElementObject:
        return SvgElementObject(
            id=uuid4(),
            name=name if name is not None else f"{self.name} 副本",
            element_kind=self.element_kind,
            svg=self.svg,
            intrinsic_bounds=self.intrinsic_bounds,
            transform=self.transform,
            transform_pivot=self.transform_pivot,
            fill_override=self.fill_override,
            stroke_override=self.stroke_override,
            stroke_width_override=self.stroke_width_override,
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
        )


@dataclass(slots=True)
class RasterGroupLayer:
    """A top-level container for raster children.

    Groups are deliberately shallow: a raster group can only contain
    :class:`RasterLayer` instances and is never itself a child of another
    group. Keeping this type separate from ``RasterLayer`` defines the
    two-level tree while ``Document.layers`` remains the canonical flat view.
    """

    id: UUID
    name: str
    children: list[RasterLayer] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False
    _revision: int = field(default=0, init=False, repr=False)

    group_kind: LayerGroupKind = field(default=LayerGroupKind.RASTER, init=False)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("raster group name must not be empty")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("raster group opacity must be between 0 and 1")
        ids = [child.id for child in self.children]
        if len(ids) != len(set(ids)):
            raise ValueError("raster layer ids must be unique within a group")

    @classmethod
    def create(cls, name: str = "栅格组") -> RasterGroupLayer:
        return cls(id=uuid4(), name=name)

    @property
    def revision(self) -> int:
        return self._revision

    def record_change(self) -> None:
        self._revision += 1

    def child_by_id(self, layer_id: UUID) -> RasterLayer:
        for child in self.children:
            if child.id == layer_id:
                return child
        raise ValueError("raster layer must belong to the group")

    def duplicate(self, name: str | None = None) -> RasterGroupLayer:
        return RasterGroupLayer(
            id=uuid4(),
            name=name if name is not None else f"{self.name} 副本",
            children=[child.duplicate() for child in self.children],
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
        )


@dataclass(slots=True)
class VectorGroupLayer:
    id: UUID
    name: str
    children: list[TextObject | SvgElementObject] = field(default_factory=list)
    visible: bool = True
    opacity: float = 1.0
    locked: bool = False
    _revision: int = field(default=0, init=False, repr=False)

    group_kind: LayerGroupKind = field(default=LayerGroupKind.VECTOR, init=False)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("vector group name must not be empty")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("vector group opacity must be between 0 and 1")
        ids: set[UUID] = set()
        for child in self.children:
            if child.id in ids:
                raise ValueError("text object ids must be unique within a group")
            ids.add(child.id)

    @classmethod
    def create(cls, name: str = "文字组") -> VectorGroupLayer:
        return cls(id=uuid4(), name=name)

    @property
    def revision(self) -> int:
        return self._revision

    def record_change(self) -> None:
        self._revision += 1

    def child_by_id(self, object_id: UUID) -> TextObject | SvgElementObject:
        for child in self.children:
            if child.id == object_id:
                return child
        raise ValueError("text object must belong to the group")

    def duplicate(self, name: str | None = None) -> VectorGroupLayer:
        return VectorGroupLayer(
            id=uuid4(),
            name=name if name is not None else f"{self.name} 副本",
            children=[child.duplicate() for child in self.children],
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
        )


VectorObject = TextObject | SvgElementObject
LayerGroup = RasterGroupLayer | VectorGroupLayer


__all__ = [
    "LayerGroupKind",
    "LayerGroup",
    "MAX_FONT_FAMILY_LENGTH",
    "MAX_TEXT_FONT_SIZE",
    "MAX_TEXT_LENGTH",
    "MAX_SVG_FRAGMENT_BYTES",
    "RGBAColor",
    "TextHorizontalAlignment",
    "TextObject",
    "SvgElementObject",
    "TextVerticalAlignment",
    "TextWrapMode",
    "VectorObject",
    "VectorObjectKind",
    "RasterGroupLayer",
    "VectorGroupLayer",
]
