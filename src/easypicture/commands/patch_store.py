"""Compressed storage for undoable full-Tile before/after snapshots."""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np

from easypicture.pixels import RGBAArray
from easypicture.pixels.types import validate_rgba_array


@dataclass(frozen=True, slots=True)
class PatchHandle:
    key: int | None
    allocated: bool
    codec: str
    stored_bytes: int
    uncompressed_bytes: int
    shape: tuple[int, int, int] | None


class UndoPatchStore:
    """Own compressed blobs referenced by undo commands."""

    def __init__(self) -> None:
        self._blobs: dict[int, bytes] = {}
        self._next_key = 1
        self._allocated_bytes = 0

    @property
    def allocated_bytes(self) -> int:
        return self._allocated_bytes

    @property
    def handle_count(self) -> int:
        return len(self._blobs)

    def capture(self, pixels: RGBAArray | None) -> PatchHandle:
        if pixels is None:
            return PatchHandle(
                key=None,
                allocated=False,
                codec="missing",
                stored_bytes=0,
                uncompressed_bytes=0,
                shape=None,
            )
        validate_rgba_array(pixels)
        raw = pixels.tobytes(order="C")
        compressed = zlib.compress(raw, level=1)
        if len(compressed) < len(raw):
            codec = "zlib"
            payload = compressed
        else:
            codec = "raw"
            payload = raw
        key = self._next_key
        self._next_key += 1
        self._blobs[key] = payload
        self._allocated_bytes += len(payload)
        return PatchHandle(
            key=key,
            allocated=True,
            codec=codec,
            stored_bytes=len(payload),
            uncompressed_bytes=len(raw),
            shape=(int(pixels.shape[0]), int(pixels.shape[1]), 4),
        )

    def restore(self, handle: PatchHandle) -> RGBAArray | None:
        if not handle.allocated:
            return None
        if handle.key is None or handle.shape is None:
            raise RuntimeError("allocated patch handle is incomplete")
        try:
            payload = self._blobs[handle.key]
        except KeyError as exc:
            raise RuntimeError("undo patch is no longer available") from exc
        if handle.codec == "zlib":
            raw = zlib.decompress(payload)
        elif handle.codec == "raw":
            raw = payload
        else:
            raise RuntimeError(f"unsupported undo patch codec: {handle.codec}")
        if len(raw) != handle.uncompressed_bytes:
            raise RuntimeError("undo patch size validation failed")
        return np.frombuffer(raw, dtype=np.uint8).reshape(handle.shape).copy(order="C")

    def release(self, handle: PatchHandle) -> None:
        if handle.key is None:
            return
        payload = self._blobs.pop(handle.key, None)
        if payload is not None:
            self._allocated_bytes -= len(payload)

    def clear(self) -> None:
        self._blobs.clear()
        self._allocated_bytes = 0


__all__ = ["PatchHandle", "UndoPatchStore"]
