"""Single-document aggregate and lifecycle events."""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID, uuid4

from easypicture.domain.events import (
    ActiveLayerChanged,
    DocumentEvent,
    DocumentEventListener,
    DocumentModifiedChanged,
    DocumentRevisionChanged,
)
from easypicture.domain.geometry import Size
from easypicture.domain.layer import RasterLayer
from easypicture.domain.selection import Selection

MAX_DOCUMENT_DIMENSION = 8192
type Unsubscribe = Callable[[], None]


class Document:
    """Qt-free aggregate for one EasyPicture document."""

    def __init__(
        self,
        *,
        document_id: UUID,
        size: Size,
        layers: list[RasterLayer],
        active_layer_id: UUID,
        selection: Selection,
    ) -> None:
        if not layers:
            raise ValueError("document must contain at least one layer")
        if all(layer.id != active_layer_id for layer in layers):
            raise ValueError("active layer must belong to the document")
        self.id = document_id
        self.size = size
        self._layers = layers
        self._active_layer_id = active_layer_id
        self.selection = selection
        self._modified = False
        self._revision = 0
        self._listeners: dict[int, DocumentEventListener] = {}
        self._next_listener_token = 0

    @classmethod
    def create(cls, width: int, height: int) -> Document:
        if not 1 <= width <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document width must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        if not 1 <= height <= MAX_DOCUMENT_DIMENSION:
            raise ValueError(f"document height must be between 1 and {MAX_DOCUMENT_DIMENSION}")
        size = Size(width, height)
        layer = RasterLayer.create_blank("图层 1", size)
        return cls(
            document_id=uuid4(),
            size=size,
            layers=[layer],
            active_layer_id=layer.id,
            selection=Selection.create_empty(size),
        )

    @property
    def width(self) -> int:
        return self.size.width

    @property
    def height(self) -> int:
        return self.size.height

    @property
    def layers(self) -> tuple[RasterLayer, ...]:
        return tuple(self._layers)

    @property
    def active_layer_id(self) -> UUID:
        return self._active_layer_id

    @property
    def active_layer(self) -> RasterLayer:
        for layer in self._layers:
            if layer.id == self._active_layer_id:
                return layer
        raise RuntimeError("active layer invariant is broken")

    @property
    def modified(self) -> bool:
        return self._modified

    @property
    def revision(self) -> int:
        return self._revision

    def subscribe(self, listener: DocumentEventListener) -> Unsubscribe:
        token = self._next_listener_token
        self._next_listener_token += 1
        self._listeners[token] = listener

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def record_change(self) -> None:
        self._revision += 1
        if not self._modified:
            self._modified = True
            self._emit(
                DocumentModifiedChanged(
                    document_id=self.id,
                    revision=self._revision,
                    modified=True,
                )
            )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=self._revision))

    def mark_clean(self) -> None:
        if not self._modified:
            return
        self._modified = False
        self._emit(
            DocumentModifiedChanged(
                document_id=self.id,
                revision=self._revision,
                modified=False,
            )
        )

    def set_active_layer(self, layer_id: UUID) -> None:
        if layer_id == self._active_layer_id:
            return
        if all(layer.id != layer_id for layer in self._layers):
            raise ValueError("active layer must belong to the document")
        self._active_layer_id = layer_id
        self._revision += 1
        self._emit(
            ActiveLayerChanged(
                document_id=self.id,
                revision=self._revision,
                layer_id=layer_id,
            )
        )
        self._emit(DocumentRevisionChanged(document_id=self.id, revision=self._revision))

    def _emit(self, event: DocumentEvent) -> None:
        for listener in tuple(self._listeners.values()):
            listener(event)


__all__ = ["Document", "MAX_DOCUMENT_DIMENSION", "Unsubscribe"]
