"""Copy-on-write document snapshots safe for persistence workers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from easypicture.domain import (
    AffineTransform,
    Collection,
    Document,
    DocumentKind,
    DocumentNode,
    FloatRect,
    PixelPoint,
    Point,
    RasterObject,
    Selection,
    Size,
    SpriteAnimation,
    SpriteFrameDefinition,
    SpriteWorkspace,
    SvgObject,
    TextHorizontalAlignment,
    TextObject,
    TextVerticalAlignment,
    TextWrapMode,
)
from easypicture.domain.layer import RasterSampling
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
    transform: AffineTransform = AffineTransform()
    transform_pivot: Point | None = None
    sampling: RasterSampling = RasterSampling.NEAREST

    @property
    def world_transform(self) -> AffineTransform:
        return self.transform.then(AffineTransform.translation(self.origin.x, self.origin.y))

    @classmethod
    def capture(cls, layer: RasterObject) -> LayerSnapshot:
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
            transform=layer.transform,
            transform_pivot=layer.transform_pivot,
            sampling=layer.sampling,
        )

    def to_layer(self) -> RasterObject:
        return RasterObject(
            id=self.id,
            name=self.name,
            size=self.size,
            origin=self.origin,
            pixels=self.pixels.clone_shared(),
            visible=self.visible,
            opacity=self.opacity,
            locked=self.locked,
            alpha_locked=self.alpha_locked,
            transform=self.transform,
            transform_pivot=self.transform_pivot,
            sampling=self.sampling,
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
    def capture(cls, element: SvgObject) -> SvgElementSnapshot:
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

    def to_object(self) -> SvgObject:
        return SvgObject(
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
class CollectionSnapshot:
    id: UUID
    name: str
    children: tuple[DocumentNodeSnapshot, ...]
    visible: bool
    opacity: float
    locked: bool

    @classmethod
    def capture(cls, group: Collection) -> CollectionSnapshot:
        return cls(
            group.id,
            group.name,
            tuple(capture_node(child) for child in group.children),
            group.visible,
            group.opacity,
            group.locked,
        )

    def to_group(self) -> Collection:
        return Collection(
            self.id,
            self.name,
            [restore_node(child) for child in self.children],
            self.visible,
            self.opacity,
            self.locked,
        )


type DocumentNodeSnapshot = CollectionSnapshot | LayerSnapshot | VectorObjectSnapshot


def capture_node(node: DocumentNode) -> DocumentNodeSnapshot:
    if isinstance(node, Collection):
        return CollectionSnapshot.capture(node)
    if isinstance(node, RasterObject):
        return LayerSnapshot.capture(node)
    if isinstance(node, TextObject):
        return TextObjectSnapshot.capture(node)
    return SvgElementSnapshot.capture(node)


def restore_node(node: DocumentNodeSnapshot) -> DocumentNode:
    if isinstance(node, CollectionSnapshot):
        return node.to_group()
    if isinstance(node, LayerSnapshot):
        return node.to_layer()
    return node.to_object()


def walk_snapshot_nodes(
    nodes: tuple[DocumentNodeSnapshot, ...],
) -> tuple[DocumentNodeSnapshot, ...]:
    result: list[DocumentNodeSnapshot] = []
    pending = list(reversed(nodes))
    while pending:
        node = pending.pop()
        result.append(node)
        if isinstance(node, CollectionSnapshot):
            pending.extend(reversed(node.children))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class SpriteWorkspaceSnapshot:
    cell_size: Size
    frames: tuple[SpriteFrameDefinition, ...]
    animations: tuple[SpriteAnimation, ...] = ()

    @property
    def ungrouped_frame_ids(self) -> tuple[UUID, ...]:
        assigned = {frame_id for animation in self.animations for frame_id in animation.frame_ids}
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
    active_layer_id: UUID | None
    selection: MaskTileStore
    project_path: Path | None
    source_name: str | None
    nodes: tuple[DocumentNodeSnapshot, ...]
    kind: DocumentKind = DocumentKind.PROJECT
    sprite_workspace: SpriteWorkspaceSnapshot | None = None
    embedded_documents: tuple[DocumentSnapshot, ...] = ()

    def require_raster_target(self) -> UUID:
        if self.active_layer_id is None:
            raise ValueError("no editable raster object is selected")
        return self.active_layer_id

    @property
    def raster_layers(self) -> tuple[LayerSnapshot, ...]:
        return tuple(
            node for node in walk_snapshot_nodes(self.nodes) if isinstance(node, LayerSnapshot)
        )

    @classmethod
    def capture(cls, document: Document) -> DocumentSnapshot:
        from easypicture.diagnostics import performance_counters

        performance_counters.snapshots += 1
        return cls(
            document_id=document.id,
            revision=document.revision,
            size=document.size,
            active_layer_id=document.active_layer_id,
            selection=document.selection.mask.clone_shared(),
            project_path=document.project_path,
            source_name=document.source_name,
            nodes=tuple(capture_node(node) for node in document.nodes),
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
        nodes = [restore_node(node) for node in self.nodes]
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
                self.sprite_workspace.to_workspace() if self.sprite_workspace is not None else None
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
    "CollectionSnapshot",
    "SvgElementSnapshot",
    "SpriteWorkspaceSnapshot",
    "TextObjectSnapshot",
    "VectorObjectSnapshot",
]
