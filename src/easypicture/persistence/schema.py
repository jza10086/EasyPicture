"""Current EasyPicture project schema contract."""

from __future__ import annotations

import copy
from typing import Any

PROJECT_SCHEMA_VERSION = 4


def require_current_schema(manifest: dict[str, Any]) -> None:
    """Reject every project schema except the current pre-release contract."""
    version = manifest.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("manifest schema_version must be an integer")
    if version != PROJECT_SCHEMA_VERSION:
        raise ValueError("project schema version is unsupported")


def migrate_to_current_schema(manifest: dict[str, Any]) -> dict[str, Any]:
    """Migrate released schema v1/v2/v3 projects to the current schema."""
    version = manifest.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("manifest schema_version must be an integer")
    if version == PROJECT_SCHEMA_VERSION:
        return manifest
    if version not in {1, 2, 3}:
        raise ValueError("project schema version is unsupported")
    canvas = manifest.get("canvas")
    if not isinstance(canvas, dict):
        raise ValueError("project canvas is invalid")
    width = canvas.get("width")
    height = canvas.get("height")
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
    ):
        raise ValueError("project canvas is invalid")
    migrated = copy.deepcopy(manifest)
    if version == 1:
        migrated["sprite_workspace"] = {
            "cell_size": {"width": min(32, width), "height": min(32, height)},
            "frames": [],
        }
    workspace = migrated.get("sprite_workspace")
    if not isinstance(workspace, dict):
        raise ValueError("project sprite workspace is invalid")
    frames = workspace.get("frames")
    if not isinstance(frames, list):
        raise ValueError("project sprite frames are invalid")
    for frame in frames:
        if not isinstance(frame, dict):
            raise ValueError("project sprite frame is invalid")
        frame.setdefault("content_mode", "linked")
        frame.setdefault("embedded_document_id", None)
        frame.setdefault("embedded_path", None)
        frame.setdefault("duration_ticks", 1)
    if version <= 2:
        workspace.setdefault("groups", [])
    groups = workspace.pop("groups", None)
    if groups is not None:
        if not isinstance(groups, list):
            raise ValueError("project sprite frame groups are invalid")
        workspace["animations"] = [
            {
                **group,
                "fps": 12,
                "playback_mode": "loop",
            }
            for group in groups
            if isinstance(group, dict)
        ]
        if len(workspace["animations"]) != len(groups):
            raise ValueError("project sprite frame groups are invalid")
    else:
        workspace.setdefault("animations", [])
    migrated["document_kind"] = "project"
    migrated["schema_version"] = PROJECT_SCHEMA_VERSION
    return migrated


__all__ = [
    "PROJECT_SCHEMA_VERSION",
    "migrate_to_current_schema",
    "require_current_schema",
]
