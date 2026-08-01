"""Versioned manifest migration boundary."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import NAMESPACE_URL, uuid5

CURRENT_SCHEMA_VERSION = 3
type Manifest = dict[str, Any]
type Migration = Callable[[Manifest], Manifest]
def _migrate_v1_to_v2(manifest: Manifest) -> Manifest:
    """Wrap legacy raster layers in the v2 root-node representation."""
    current = dict(manifest)
    if "nodes" not in current:
        legacy_layers = current.get("layers")
        if not isinstance(legacy_layers, list):
            raise ValueError("v1 manifest layers must be a list")
        current["nodes"] = [
            {**layer, "type": "raster"}
            if isinstance(layer, dict)
            else layer
            for layer in legacy_layers
        ]
    return current


def _migrate_v2_to_v3(manifest: Manifest) -> Manifest:
    """Convert P9A root leaves/text groups into exactly two-level groups."""
    current = dict(manifest)
    raw_nodes = current.get("nodes", current.get("layers"))
    if not isinstance(raw_nodes, list):
        raise ValueError("v2 manifest nodes must be a list")
    document_id = str(current.get("document_id", "unknown"))
    groups: list[dict[str, Any]] = []
    raster_run: list[dict[str, Any]] = []

    def flush_raster_run() -> None:
        if not raster_run:
            return
        index = len(groups)
        group_id = str(uuid5(NAMESPACE_URL, f"easypicture:raster-group:{document_id}:{index}"))
        group_name = "栅格组" if index == 0 else f"栅格组.{index}"
        groups.append(
            {
                "type": "raster_group",
                "id": group_id,
                "name": group_name,
                "visible": True,
                "opacity": 1.0,
                "locked": False,
                "children": list(raster_run),
            }
        )
        raster_run.clear()

    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise ValueError("v2 manifest node must be an object")
        node_type = raw_node.get("type", "raster")
        if node_type == "raster":
            raster_run.append(dict(raw_node))
            continue
        flush_raster_run()
        if node_type == "text_group":
            group = dict(raw_node)
            group["type"] = "vector_group"
            groups.append(group)
        else:
            raise ValueError(f"unknown v2 node type: {node_type}")
    flush_raster_run()
    current["nodes"] = groups
    current["schema_version"] = 3
    return current


MIGRATIONS: dict[int, Migration] = {1: _migrate_v1_to_v2, 2: _migrate_v2_to_v3}


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
