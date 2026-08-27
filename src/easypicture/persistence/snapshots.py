"""Copy-on-write document snapshots safe for persistence workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from easypicture.domain import (
    AffineTransform,
    Document,
    DocumentKind,
    FloatRect,
    LayerGroupKind,
    PixelPoint,
    Point,
    RasterGroupLayer,
    RasterLayer,
    Selection,
    Size,
    SpriteAnimation,
    SpriteFrameDefinition,
    SpriteWorkspace,
    SvgElementObject,
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
class SvgElementSnapshot:
    id: UUID
    name: str
    element_kind: str
    svg: str
    intrinsic_bounds: FloatRect
    transform: AffineTransform
    transform_pivot: Point | None
    fill_override: tuple[int, int, int, int] | None
    stroke_override: tuple[int, int, int, int] | None
    stroke_width_override: float | None
    visible: bool
    opacity: float
    locked: bool

    @classmethod
    def capture(cls, element: SvgElementObject) -> SvgElementSnapshot:
        return cls(
            id=element.id,
            name=element.name,
            element_kind=element.element_kind,
            svg=element.svg,
            intrinsic_bounds=element.intrinsic_bounds,
            transform=element.transform,
            transform_pivot=element.transform_pivot,
            fill_override=element.fill_override,
            stroke_override=element.stroke_override,
            stroke_width_override=element.stroke_width_override,
            visible=element.visible,
            opacity=element.opacity,
            locked=element.locked,
        )

    def to_object(self) -> SvgElementObject:
        return SvgElementObject(
            id=self.id,
            name=self.name,
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


VectorObjectSnapshot = TextObjectSnapshot | SvgElementSnapshot


@dataclass(frozen=True, slots=True)
class VectorGroupSnapshot:
    id: UUID
    name: str
    children: tuple[VectorObjectSnapshot, ...]
    visible: bool
    opacity: float
    locked: bool
    kind: LayerGroupKind = LayerGroupKind.VECTOR

    @classmethod
    def capture(cls, group: VectorGroupLayer) -> VectorGroupSnapshot:
        return cls(
            id=group.id,
            name=group.name,
            children=tuple(
                TextObjectSnapshot.capture(child)
                if isinstance(child, TextObject)
                else SvgElementSnapshot.capture(child)
                for child in group.children
            ),
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
    kind: LayerGroupKind = LayerGroupKind.RASTER

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
class SpriteWorkspaceSnapshot:
    cell_size: Size
    frames: tuple[SpriteFrameDefinition, ...]
    animations: tuple[SpriteAnimation, ...] = ()

    @property
    def ungrouped_frame_ids(self) -> tuple[UUID, ...]:
        assigned = {
            frame_id
            for animation in self.animations
            for frame_id in animation.frame_ids
        }
        return tuple(frame.id for frame in self.frames if frame.id not in assigned)

    @classmethod
    def capture(cls, workspace: SpriteWorkspace) -> SpriteWorkspaceSnapshot:
        return cls(
            workspace.cell_size,
            tuple(workspace.frames),
            tuple(workspace.animations),
        )

    def to_workspace(self) -> SpriteWorkspace:
        return SpriteWorkspace(
            self.cell_size,
            list(self.frames),
            list(self.animations),
        )

@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    document_id: UUID
    revision: int
    size: Size
    active_layer_id: UUID
    selection: MaskTileStore
    project_path: Path | None
    source_name: str | None
    nodes: tuple[DocumentNodeSnapshot, ...]
    kind: DocumentKind = DocumentKind.PROJECT
    sprite_workspace: SpriteWorkspaceSnapshot | None = None
    embedded_documents: tuple[DocumentSnapshot, ...] = ()

    @property
    def raster_layers(self) -> tuple[LayerSnapshot, ...]:
        return tuple(
            child
            for node in self.nodes
            if isinstance(node, RasterGroupSnapshot)
            for child in node.children
        )

    @classmethod
    def capture(cls, document: Document) -> DocumentSnapshot:
        return cls(
            document_id=document.id,
            revision=document.revision,
            size=document.size,
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
            kind=document.kind,
            sprite_workspace=(
                SpriteWorkspaceSnapshot.capture(document.sprite_workspace)
                if document.kind is DocumentKind.PROJECT
                else None
            ),
            embedded_documents=tuple(
                cls.capture(item)
                for _, item in sorted(
                    document.embedded_documents.items(),
                    key=lambda pair: str(pair[0]),
                )
            ),
        )

    def to_document(
        self,
        *,
        project_path: Path | None = None,
        modified: bool = False,
    ) -> Document:
        nodes = [node.to_group() for node in self.nodes]
        embedded = {
            snapshot.document_id: snapshot.to_document(modified=modified)
            for snapshot in self.embedded_documents
        }
        return Document.restore(
            document_id=self.document_id,
            size=self.size,
            active_layer_id=self.active_layer_id,
            selection=Selection(size=self.size, mask=self.selection.clone_shared()),
            nodes=nodes,
            sprite_workspace=(
                self.sprite_workspace.to_workspace()
                if self.sprite_workspace is not None
                else None
            ),
            kind=self.kind,
            embedded_documents=embedded,
            project_path=project_path,
            source_name=self.source_name,
            modified=modified,
        )


__all__ = [
    "DocumentNodeSnapshot",
    "DocumentSnapshot",
    "LayerSnapshot",
    "RasterGroupSnapshot",
    "SvgElementSnapshot",
    "SpriteWorkspaceSnapshot",
    "TextObjectSnapshot",
    "VectorGroupSnapshot",
    "VectorObjectSnapshot",
]
