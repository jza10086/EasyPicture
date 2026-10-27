"""Current-only project schema. Older projects are never rewritten or migrated."""

from typing import Any

PROJECT_SCHEMA_VERSION = 5


def require_current_schema(manifest: dict[str, Any]) -> None:
    version = manifest.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("manifest schema_version must be an integer")
    if version != PROJECT_SCHEMA_VERSION:
        raise ValueError("project schema version is unsupported; only schema v5 is accepted")


__all__ = ["PROJECT_SCHEMA_VERSION", "require_current_schema"]
