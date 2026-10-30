"""P4 brush latency and 4096 paint-bucket benchmark."""

from __future__ import annotations

import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import psutil

from easypicture.domain import Document
from easypicture.imaging import (
    BrushSettings,
    BrushStrokeEngine,
    FillRequest,
    RegionQuery,
    calculate_fill,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import MaskArray, RGBAArray, TileCoord, TilePixelStore

OUTPUT_PATH = (
    Path(
        os.environ.get(
            "EASYPICTURE_BENCHMARK_DIR",
            str(Path(__file__).resolve().parents[1] / "runs" / "benchmarks"),
        )
    )
    / "p4.json"
)
DIMENSION = 4096


class BenchmarkSurface:
    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self.origin_x = 0
        self.origin_y = 0
        self.document_width = width
        self.document_height = height
        self._source = TilePixelStore()
        self._working: dict[TileCoord, RGBAArray] = {}

    def writable_tile(self, coord: TileCoord) -> RGBAArray:
        tile = self._working.get(coord)
        if tile is None:
            tile = self._source.read_tile(coord.x, coord.y).copy(order="C")
            self._working[coord] = tile
        return tile

    def selection_region(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> MaskArray | None:
        _ = x, y, width, height
        return None


def percentile_95(values: list[float]) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), 95))


def benchmark_brush(size: int) -> dict[str, float]:
    surface = BenchmarkSurface(DIMENSION, DIMENSION)
    stroke = BrushStrokeEngine(
        surface,
        BrushSettings(size=size, hardness=0.8, spacing=0.15),
        (20, 80, 180, 200),
    )
    stroke.begin(256.0, 512.0)
    samples: list[float] = []
    for index in range(1, 101):
        started = time.perf_counter_ns()
        stroke.extend(256.0 + index * 24.0, 512.0 + (index % 5) * 2.0)
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile_95(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def benchmark_fill() -> dict[str, int | float]:
    document = Document.create(DIMENSION, DIMENSION)
    snapshot = DocumentSnapshot.capture(document)
    progress_updates = 0

    def progress(_value: int, _maximum: int, _message: str) -> None:
        nonlocal progress_updates
        progress_updates += 1

    started = time.perf_counter_ns()
    result = calculate_fill(
        FillRequest(
            snapshot=snapshot,
            layer_id=document.active_layer_id,
            query=RegionQuery((0, 0)),
            color=(200, 40, 20, 255),
            edge_expansion=1,
        ),
        CancellationToken(),
        progress,
    )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "updated_tiles": len(result.updates),
        "updated_bytes": sum(tile.nbytes for tile in result.updates.values()),
        "progress_updates": progress_updates,
    }


def main() -> int:
    process = psutil.Process()
    rss_before = process.memory_info().rss
    brush = {str(size): benchmark_brush(size) for size in (32, 128, 256)}
    fill = benchmark_fill()
    rss_after = process.memory_info().rss
    results: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": {
            "python": sys.version,
            "system": platform.platform(),
            "processor": platform.processor(),
            "physical_memory_bytes": psutil.virtual_memory().total,
        },
        "scenario": {
            "dimension": DIMENSION,
            "brush_event_distance_px": 24,
            "brush_events": 100,
        },
        "brush": brush,
        "fill": fill,
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
