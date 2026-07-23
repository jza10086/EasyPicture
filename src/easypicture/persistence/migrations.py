"""Versioned manifest migration boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

CURRENT_SCHEMA_VERSION = 1
type Manifest = dict[str, Any]
type Migration = Callable[[Manifest], Manifest]
MIGRATIONS: dict[int, Migration] = {}


def migrate_manifest(manifest: Manifest) -> Manifest:
    raw_version = manifest.get("schema_version")
    if not isinstance(raw_version, int):
        raise ValueError("manifest schema_version must be an integer")
    if raw_version > CURRENT_SCHEMA_VERSION:
        raise ValueError(f"unsupported future schema version: {raw_version}")
    current = dict(manifest)
    version = raw_version
    while version < CURRENT_SCHEMA_VERSION:
        migration = MIGRATIONS.get(version)
        if migration is None:
            raise ValueError(f"no migration is available for schema version {version}")
        current = migration(current)
        version += 1
        current["schema_version"] = version
    return current


__all__ = ["CURRENT_SCHEMA_VERSION", "MIGRATIONS", "Manifest", "migrate_manifest"]
