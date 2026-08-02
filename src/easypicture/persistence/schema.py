"""Current EasyPicture project schema contract."""

from __future__ import annotations

from typing import Any

PROJECT_SCHEMA_VERSION = 1


def require_current_schema(manifest: dict[str, Any]) -> None:
    """Reject every project schema except the current pre-release contract."""
    version = manifest.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("manifest schema_version must be an integer")
    if version != PROJECT_SCHEMA_VERSION:
        raise ValueError("project schema version is unsupported")


__all__ = ["PROJECT_SCHEMA_VERSION", "require_current_schema"]
