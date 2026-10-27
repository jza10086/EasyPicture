"""Application-owned temporary files and conservative stale cleanup."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

OWNERSHIP_MARKER = ".easypicture-owned"
OWNERSHIP_SIGNATURE = "EasyPicture temporary data\n"
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


@dataclass(frozen=True, slots=True)
class TempCleanupReport:
    removed_entries: int = 0
    removed_bytes: int = 0
    failures: tuple[str, ...] = ()


class OwnedTemporaryDirectory:
    """TemporaryDirectory-compatible object rooted in EasyPicture's cache."""

    def __init__(self, manager: TemporaryFileManager, prefix: str) -> None:
        self._manager = manager
        self._path: Path | None = manager.create_job_directory(prefix)
        self.name = str(self._path)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_args: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._path is None:
            return
        path = self._path
        self._path = None
        self._manager.remove_owned_directory(path)


class TemporaryFileManager:
    """Own one narrow cache root and never deletes outside it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._lock = threading.RLock()
        self._active: set[Path] = set()
        self._ensure_root()

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / OWNERSHIP_MARKER
        if marker.exists():
            if marker.read_text(encoding="utf-8") != OWNERSHIP_SIGNATURE:
                raise RuntimeError(f"temporary root has an invalid ownership marker: {self.root}")
            return
        marker.write_text(OWNERSHIP_SIGNATURE, encoding="utf-8")

    def create_job_directory(self, prefix: str) -> Path:
        safe_prefix = _safe_prefix(prefix)
        with self._lock:
            self._ensure_root()
            path = self.root / f"{safe_prefix}{uuid4().hex}"
            path.mkdir()
            (path / OWNERSHIP_MARKER).write_text(OWNERSHIP_SIGNATURE, encoding="utf-8")
            self._active.add(path)
            return path

    def create_file(self, prefix: str, suffix: str) -> Path:
        safe_prefix = _safe_prefix(prefix)
        with self._lock:
            self._ensure_root()
            descriptor, raw_path = tempfile.mkstemp(
                prefix=safe_prefix,
                suffix=suffix,
                dir=self.root,
            )
            os.close(descriptor)
            path = Path(raw_path).resolve()
            self._active.add(path)
            return path

    def release_file(self, path: Path, *, delete: bool = True) -> None:
        candidate = Path(path).resolve()
        if not _is_child(candidate, self.root):
            raise ValueError("temporary file is outside the EasyPicture cache root")
        with self._lock:
            self._active.discard(candidate)
        if delete:
            candidate.unlink(missing_ok=True)

    def remove_owned_directory(self, path: Path) -> None:
        candidate = Path(path).resolve()
        if not _is_child(candidate, self.root):
            raise ValueError("temporary directory is outside the EasyPicture cache root")
        marker = candidate / OWNERSHIP_MARKER
        if not _has_valid_marker(marker):
            raise ValueError("temporary directory has no EasyPicture ownership marker")
        with self._lock:
            self._active.discard(candidate)
        shutil.rmtree(candidate, ignore_errors=False)

    def cleanup_stale(
        self,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        now: float | None = None,
    ) -> TempCleanupReport:
        self._ensure_root()
        cutoff = (time.time() if now is None else now) - max_age_seconds
        removed_entries = 0
        removed_bytes = 0
        failures: list[str] = []
        with self._lock:
            active = set(self._active)
        for entry in self.root.iterdir():
            if entry.name == OWNERSHIP_MARKER or entry.resolve() in active:
                continue
            try:
                if entry.stat().st_mtime > cutoff:
                    continue
                if entry.is_dir():
                    if not _has_valid_marker(entry / OWNERSHIP_MARKER):
                        continue
                    size = _path_size(entry)
                    shutil.rmtree(entry)
                elif entry.name.startswith("easypicture-"):
                    size = entry.stat().st_size
                    entry.unlink()
                else:
                    continue
                removed_entries += 1
                removed_bytes += size
            except OSError as error:
                failures.append(f"{entry}: {error}")
        return TempCleanupReport(removed_entries, removed_bytes, tuple(failures))

    def cleanup_current_session(self) -> TempCleanupReport:
        with self._lock:
            active = tuple(self._active)
        removed_entries = 0
        removed_bytes = 0
        failures: list[str] = []
        for entry in active:
            try:
                size = _path_size(entry) if entry.exists() else 0
                if entry.is_dir():
                    self.remove_owned_directory(entry)
                else:
                    self.release_file(entry)
                removed_entries += 1
                removed_bytes += size
            except OSError as error:
                failures.append(f"{entry}: {error}")
        return TempCleanupReport(removed_entries, removed_bytes, tuple(failures))


_manager: TemporaryFileManager | None = None
_manager_lock = threading.RLock()


def configure_temp_root(root: Path) -> TemporaryFileManager:
    global _manager
    with _manager_lock:
        _manager = TemporaryFileManager(root)
        return _manager


def current_temp_manager() -> TemporaryFileManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            fallback = Path(tempfile.gettempdir()) / "EasyPicture" / "temp"
            _manager = TemporaryFileManager(fallback)
        return _manager


def owned_temporary_directory(prefix: str) -> OwnedTemporaryDirectory:
    return OwnedTemporaryDirectory(current_temp_manager(), prefix)


def temporary_file(prefix: str, suffix: str) -> Path:
    return current_temp_manager().create_file(prefix, suffix)


def release_temporary_file(path: Path) -> None:
    current_temp_manager().release_file(path)


def cleanup_stale_sibling_temps(
    destination: Path,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> TempCleanupReport:
    """Remove only stale atomic-write siblings matching one exact target."""
    target = Path(destination)
    parent = target.parent
    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    removed_bytes = 0
    failures: list[str] = []
    pattern = f".{target.name}.*.tmp"
    for candidate in parent.glob(pattern):
        try:
            if not candidate.is_file() or candidate.stat().st_mtime > cutoff:
                continue
            size = candidate.stat().st_size
            candidate.unlink()
            removed += 1
            removed_bytes += size
        except OSError as error:
            failures.append(f"{candidate}: {error}")
    return TempCleanupReport(removed, removed_bytes, tuple(failures))


def _safe_prefix(prefix: str) -> str:
    normalized = "".join(
        character for character in prefix if character.isalnum() or character in "-_"
    )
    if not normalized:
        normalized = "easypicture-"
    if not normalized.startswith("easypicture-"):
        normalized = f"easypicture-{normalized}"
    return normalized


def _has_valid_marker(marker: Path) -> bool:
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8") == OWNERSHIP_SIGNATURE
    except OSError:
        return False


def _is_child(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return candidate != parent


def _path_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
    return total


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "OWNERSHIP_MARKER",
    "OwnedTemporaryDirectory",
    "TempCleanupReport",
    "TemporaryFileManager",
    "cleanup_stale_sibling_temps",
    "configure_temp_root",
    "current_temp_manager",
    "owned_temporary_directory",
    "release_temporary_file",
    "temporary_file",
]
