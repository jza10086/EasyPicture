"""One-time, non-destructive workspace relocation; compare paths/sizes, never hashes."""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HISTORY = "archive/legacy-2026-10-26"
MOVES = {
    "src": "source/src",
    "scripts": "source/scripts",
    "pyproject.toml": "source/pyproject.toml",
    "uv.lock": "source/uv.lock",
    "README.md": f"{HISTORY}/README.md",
    "docs": f"{HISTORY}/docs",
    "build": f"{HISTORY}/build",
    "tests": "archive/validation/tests",
    "benchmarks": "archive/validation/benchmarks",
    "test-assets": "archive/validation/test-assets",
    ".venv": f"{HISTORY}/environment/.venv",
    **{p: f"{HISTORY}/caches/{p}" for p in (
        ".cache", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "interpreter-v4", "sdists-v9", ".lock", "CACHEDIR.TAG",
    )},
    "nuitka-crash-report.xml": f"{HISTORY}/nuitka-crash-report.xml",
}


def inside(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ValueError(f"Unsafe relocation path: {path}")
    return path


def inventory(path):
    if path.is_file():
        return {".": path.stat().st_size}
    return {
        str(p.relative_to(path)): p.stat().st_size
        for p in path.rglob("*") if p.is_file()
    }


def main():
    for old, new in MOVES.items():
        if not inside(old).exists() or inside(new).exists():
            raise ValueError(f"Source missing or destination occupied: {old} -> {new}")
    backup = inside(f"{HISTORY}/configuration")
    backup.mkdir(parents=True)
    for name in (".gitignore", "pyproject.toml", "uv.lock"):
        shutil.copy2(inside(name), backup / name)
    shutil.copytree(inside("scripts"), backup / "scripts")
    manifest = []
    report = inside("archive/migrations/2026-10-26-moves.json")
    for old, new in MOVES.items():
        src, dst = inside(old), inside(new)
        before = inventory(src)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        after = inventory(dst)
        if after != before:
            raise RuntimeError(f"Relocated file names or sizes differ: {dst}")
        manifest.append({"from": old, "to": new, "files": len(before),
                         "bytes": sum(before.values()), "paths_and_sizes_match": True})
        report.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Preserved {old} -> {new} ({len(before)} files)", flush=True)
    inside("source/docs").mkdir()
    for name in ("object_architecture.md", "limg_schema_v5.md", "object_manual_acceptance.md",
                 "ui_design_principles.md", "ui_numeric_ranges.md"):
        shutil.copy2(inside(f"{HISTORY}/docs/{name}"), inside(f"source/docs/{name}"))
    inside("release").mkdir()


if __name__ == "__main__":
    main()
