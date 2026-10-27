"""P5 4096 magic-wand and feather benchmark."""

from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

import psutil

from easypicture.domain import Document
from easypicture.imaging import (
    MagicSelectionRequest,
    RegionQuery,
    SelectionAdjustment,
    SelectionAdjustmentRequest,
    calculate_magic_selection,
    calculate_selection_adjustment,
    rectangle_candidate,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect

OUTPUT_PATH = (
    Path(os.environ.get("EASYPICTURE_BENCHMARK_DIR", "../archive/runs/benchmarks")) / "p5.json"
)
DIMENSION = 4096


def _progress(_value: int, _maximum: int, _message: str) -> None:
    return


def _elapsed(operation: object) -> tuple[object, float]:
    started = time.perf_counter_ns()
    result = operation()
    return result, (time.perf_counter_ns() - started) / 1_000_000


def benchmark_magic_wand() -> dict[str, int | float]:
    document = Document.create(DIMENSION, DIMENSION)
    request = MagicSelectionRequest(
        snapshot=DocumentSnapshot.capture(document),
        layer_id=document.active_layer_id,
        query=RegionQuery((0, 0)),
    )
    result, elapsed_ms = _elapsed(
        lambda: calculate_magic_selection(
            request,
            CancellationToken(),
            _progress,
        )
    )
    assert hasattr(result, "updates")
    updates = result.updates
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "updated_tiles": len(updates),
        "stored_bytes": sum(tile.nbytes for tile in updates.values() if tile is not None),
    }


def benchmark_feather() -> dict[str, int | float]:
    document = Document.create(DIMENSION, DIMENSION)
    candidate = rectangle_candidate(
        document.size,
        Rect(1024, 1024, 2048, 2048),
    )
    for coord, tile in candidate.items():
        document.selection.mask.replace_tile(coord.x, coord.y, tile)
    request = SelectionAdjustmentRequest(
        DocumentSnapshot.capture(document),
        SelectionAdjustment.FEATHER,
        16,
    )
    result, elapsed_ms = _elapsed(
        lambda: calculate_selection_adjustment(
            request,
            CancellationToken(),
            _progress,
        )
    )
    assert hasattr(result, "updates")
    updates = result.updates
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "updated_tiles": len(updates),
        "stored_bytes": sum(tile.nbytes for tile in updates.values() if tile is not None),
    }


def main() -> int:
    process = psutil.Process()
    rss_before = process.memory_info().rss
    wand = benchmark_magic_wand()
    feather = benchmark_feather()
    rss_after = process.memory_info().rss
    results: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": {
            "python": sys.version,
            "system": platform.platform(),
            "processor": platform.processor(),
            "physical_memory_bytes": psutil.virtual_memory().total,
        },
        "scenario": {"dimension": DIMENSION, "feather_radius": 16},
        "magic_wand": wand,
        "feather": feather,
        "rss_delta_bytes": max(0, rss_after - rss_before),
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
