"""Copy-on-write document snapshots safe for persistence workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from easypicture.domain import (
    AffineTransform,
    Document,
    FloatRect,
    LayerNodeKind,
    PixelPoint,
    Point,
    RasterGroupLayer,
    RasterLayer,
    Selection,
    Size,
    TextHorizontalAlignment,
    TextObject,
    TextVerticalAlignment,
    TextWrapMode,
    VectorGroupLayer,
)
from easypicture.pixels import MaskTileStore, TilePixelStore


@dataclass(frozen=True, slots=True)
class LayerSnapshot:
    id: UUID
    name: str
    size: Size
    origin: PixelPoint
    visible: bool
    opacity: float
    locked: bool
    alpha_locked: bool
    pixels: TilePixelStore

    @classmethod
    def capture(cls, layer: RasterLayer) -> LayerSnapshot:
        return cls(
            id=layer.id,
            name=layer.name,
            size=layer.size,
            origin=layer.origin,
            visible=layer.visible,
            opacity=layer.opacity,
            locked=layer.locked,
            alpha_locked=layer.alpha_locked,
            pixels=layer.pixels.clone_shared(),
        )

    def to_layer(self) -> RasterLayer:
        return RasterLayer(
            id=self.id,
            name=self.name,
            size=self.size,
            origin=self.origin,
            pixels=self.pixels.clone_shared(),
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
            alpha_locked=self.alpha_locked,
        )


@dataclass(frozen=True, slots=True)
class TextObjectSnapshot:
    id: UUID
    name: str
    text: str
    frame: FloatRect
    font_family: str
    font_size: float
    font_weight: int
    italic: bool
    color: tuple[int, int, int, int]
    stroke_color: tuple[int, int, int, int]
    stroke_width: float
    horizontal_alignment: TextHorizontalAlignment
    vertical_alignment: TextVerticalAlignment
    wrap_mode: TextWrapMode
    line_spacing: float
    visible: bool
    opacity: float
    locked: bool
    transform: AffineTransform
    transform_pivot: Point | None

    @classmethod
    def capture(cls, text_object: TextObject) -> TextObjectSnapshot:
        return cls(
            id=text_object.id,
            name=text_object.name,
            text=text_object.text,
            frame=text_object.frame,
            font_family=text_object.font_family,
            font_size=text_object.font_size,
            font_weight=text_object.font_weight,
            italic=text_object.italic,
            color=text_object.color,
            stroke_color=text_object.stroke_color,
            stroke_width=text_object.stroke_width,
            horizontal_alignment=text_object.horizontal_alignment,
            vertical_alignment=text_object.vertical_alignment,
            wrap_mode=text_object.wrap_mode,
            line_spacing=text_object.line_spacing,
            visible=text_object.visible,
            opacity=text_object.opacity,
            locked=text_object.locked,
            transform=text_object.transform,
            transform_pivot=text_object.transform_pivot,
        )

    def to_object(self) -> TextObject:
        return TextObject(
            id=self.id,
            name=self.name,
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


@dataclass(frozen=True, slots=True)
class VectorGroupSnapshot:
    id: UUID
    name: str
    children: tuple[TextObjectSnapshot, ...]
    visible: bool
    opacity: float
    locked: bool
    kind: LayerNodeKind = LayerNodeKind.VECTOR_GROUP

    @classmethod
    def capture(cls, group: VectorGroupLayer) -> VectorGroupSnapshot:
        return cls(
            id=group.id,
            name=group.name,
            children=tuple(TextObjectSnapshot.capture(child) for child in group.children),
            visible=group.visible,
            opacity=group.opacity,
            locked=group.locked,
        )

    def to_group(self) -> VectorGroupLayer:
        return VectorGroupLayer(
            id=self.id,
            name=self.name,
            children=[child.to_object() for child in self.children],
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
        )


@dataclass(frozen=True, slots=True)
class RasterGroupSnapshot:
    id: UUID
    name: str
    children: tuple[LayerSnapshot, ...]
    visible: bool
    opacity: float
    locked: bool
    kind: LayerNodeKind = LayerNodeKind.RASTER_GROUP

    @classmethod
    def capture(cls, group: RasterGroupLayer) -> RasterGroupSnapshot:
        return cls(
            id=group.id,
            name=group.name,
            children=tuple(LayerSnapshot.capture(child) for child in group.children),
            visible=group.visible,
            opacity=group.opacity,
            locked=group.locked,
        )

    def to_group(self) -> RasterGroupLayer:
        return RasterGroupLayer(
            id=self.id,
            name=self.name,
            children=[child.to_layer() for child in self.children],
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
        )


DocumentNodeSnapshot = RasterGroupSnapshot | VectorGroupSnapshot

@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    document_id: UUID
    revision: int
    size: Size
    layers: tuple[LayerSnapshot, ...]
    active_layer_id: UUID
    selection: MaskTileStore
    project_path: Path | None
    source_name: str | None
    nodes: tuple[DocumentNodeSnapshot, ...] = ()

    @classmethod
    def capture(cls, document: Document) -> DocumentSnapshot:
        return cls(
            document_id=document.id,
            revision=document.revision,
            size=document.size,
            layers=tuple(LayerSnapshot.capture(layer) for layer in document.layers),
            active_layer_id=document.active_layer_id,
            selection=document.selection.mask.clone_shared(),
            project_path=document.project_path,
            source_name=document.source_name,
            nodes=tuple(
                RasterGroupSnapshot.capture(node)
                if isinstance(node, RasterGroupLayer)
                else VectorGroupSnapshot.capture(node)
                for node in document.nodes
            ),
        )

    def to_document(
        self,
        *,
        project_path: Path | None = None,
        modified: bool = False,
    ) -> Document:
        nodes = [
            node.to_group()
            for node in self.nodes
        ]
        return Document.restore(
            document_id=self.document_id,
            size=self.size,
            layers=[layer.to_layer() for layer in self.layers],
            active_layer_id=self.active_layer_id,
            selection=Selection(size=self.size, mask=self.selection.clone_shared()),
            nodes=nodes or None,
            project_path=project_path,
            source_name=self.source_name,
            modified=modified,
        )


__all__ = [
    "DocumentNodeSnapshot",
    "DocumentSnapshot",
    "LayerSnapshot",
    "RasterGroupSnapshot",
    "TextObjectSnapshot",
    "VectorGroupSnapshot",
]
