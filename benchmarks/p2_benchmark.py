"""P2 dense multi-layer composition, cache, and progressive UI benchmark."""

from __future__ import annotations

import gc
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import psutil
from PySide6.QtCore import QCoreApplication, QEventLoop, QPointF, QTimer

from easypicture.domain import Document, RasterLayer
from easypicture.pixels import TileCoord
from easypicture.render import DEFAULT_RENDER_CACHE_BYTES, TileRenderer

OUTPUT_PATH = Path("build/benchmarks/p2.json")
DIMENSION = 4096
LAYER_COUNT = 10
TILES_PER_AXIS = DIMENSION // 256


def percentile_95(values: list[float]) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), 95))


def layer_color(index: int) -> tuple[int, int, int, int]:
    return (
        (index * 23 + 11) % 256,
        (index * 47 + 29) % 256,
        (index * 71 + 53) % 256,
        64 + index * 15,
    )


def build_dense_document() -> Document:
    document = Document.create(DIMENSION, DIMENSION)
    for index in range(1, LAYER_COUNT):
        document.insert_layer(
            RasterLayer.create_blank(f"基准图层 {index + 1}", document.size),
            index,
        )
    for layer_index, layer in enumerate(document.layers):
        color = layer_color(layer_index)
        for ty in range(TILES_PER_AXIS):
            for tx in range(TILES_PER_AXIS):
                tile = layer.pixels.writable_tile(tx, ty)
                tile[:] = color
    document.mark_clean()
    return document


def reference_stack(colors: list[tuple[int, int, int, int]]) -> np.ndarray:
    output_rgb = np.zeros(3, dtype=np.float64)
    output_alpha = 0.0
    for color in colors:
        source = np.asarray(color, dtype=np.float64) / 255.0
        next_alpha = source[3] + output_alpha * (1.0 - source[3])
        if next_alpha > 0:
            output_rgb = (
                source[:3] * source[3] + output_rgb * output_alpha * (1.0 - source[3])
            ) / next_alpha
        output_alpha = next_alpha
    return np.rint(np.concatenate((output_rgb, [output_alpha])) * 255).astype(np.uint8)


def benchmark_cold_tile(document: Document, iterations: int = 30) -> dict[str, float]:
    renderer = TileRenderer(document)
    samples: list[float] = []
    for _iteration in range(iterations):
        renderer.clear_cache()
        started = time.perf_counter_ns()
        image = renderer.render_tile(TileCoord(0, 0))
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
        if image.isNull():
            raise AssertionError("renderer returned a null image")
    renderer.dispose()
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile_95(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def benchmark_full_composition(document: Document) -> dict[str, int | float]:
    renderer = TileRenderer(document)
    coords = [TileCoord(tx, ty) for ty in range(TILES_PER_AXIS) for tx in range(TILES_PER_AXIS)]
    peak_cache = 0
    checksum = 0
    started = time.perf_counter_ns()
    for coord in coords:
        image = renderer.render_tile(coord)
        checksum ^= image.pixelColor(0, 0).rgba()
        peak_cache = max(peak_cache, renderer.cache_bytes)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    sample = renderer.render_tile(TileCoord(0, 0)).pixelColor(0, 0)
    actual = np.array(
        (sample.red(), sample.green(), sample.blue(), sample.alpha()),
        dtype=np.uint8,
    )
    expected = reference_stack([layer_color(index) for index in range(LAYER_COUNT)])
    max_error = int(np.abs(actual.astype(np.int16) - expected.astype(np.int16)).max())
    renderer.dispose()
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "peak_cache_bytes": peak_cache,
        "checksum": checksum,
        "max_error_lsb": max_error,
    }


def benchmark_progressive_dispatch(document: Document) -> dict[str, int | float]:
    app = QCoreApplication.instance() or QCoreApplication([])
    _ = app
    renderer = TileRenderer(document)
    coords = [TileCoord(tx, ty) for ty in range(TILES_PER_AXIS) for tx in range(TILES_PER_AXIS)]
    loop = QEventLoop()
    heartbeat = QTimer()
    heartbeat.setInterval(1)
    heartbeat_gaps: list[float] = []
    last_heartbeat = time.perf_counter_ns()
    rendered = 0

    def on_heartbeat() -> None:
        nonlocal last_heartbeat
        now = time.perf_counter_ns()
        heartbeat_gaps.append((now - last_heartbeat) / 1_000_000)
        last_heartbeat = now

    def on_tile_ready(_coord: object) -> None:
        nonlocal rendered
        rendered += 1
        if rendered == len(coords):
            loop.quit()

    heartbeat.timeout.connect(on_heartbeat)
    renderer.tile_ready.connect(on_tile_ready)
    heartbeat.start()
    started = time.perf_counter_ns()
    renderer.request_tiles(coords, QPointF(DIMENSION / 2, DIMENSION / 2))
    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(loop.quit)
    timeout.start(30_000)
    loop.exec()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    heartbeat.stop()
    timeout.stop()
    peak_cache = renderer.cache_bytes
    renderer.dispose()
    if rendered != len(coords):
        raise AssertionError(f"progressive renderer timed out after {rendered} Tiles")
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "rendered_tiles": rendered,
        "max_heartbeat_gap_ms": round(max(heartbeat_gaps, default=0.0), 3),
        "final_cache_bytes": peak_cache,
    }


def main() -> int:
    process = psutil.Process()
    gc.collect()
    rss_before = process.memory_info().rss
    started = time.perf_counter_ns()
    document = build_dense_document()
    allocation_ms = (time.perf_counter_ns() - started) / 1_000_000
    rss_dense = process.memory_info().rss
    dense_bytes = sum(layer.pixels.allocated_bytes for layer in document.layers)
    dense_tiles = sum(layer.pixels.allocated_tile_count for layer in document.layers)

    cold_tile = benchmark_cold_tile(document)
    full = benchmark_full_composition(document)
    progressive = benchmark_progressive_dispatch(document)
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
            "layers": LAYER_COUNT,
            "allocated_tiles": dense_tiles,
            "allocated_pixel_bytes": dense_bytes,
            "allocation_ms": round(allocation_ms, 3),
            "rss_delta_bytes": max(0, rss_dense - rss_before),
        },
        "cold_tile": cold_tile,
        "full_composition": full,
        "progressive": progressive,
        "acceptance": {
            "cold_tile_p95_under_16ms": cold_tile["p95_ms"] <= 16.0,
            "golden_within_1_lsb": full["max_error_lsb"] <= 1,
            "progressive_gap_under_100ms": progressive["max_heartbeat_gap_ms"] < 100.0,
            "render_cache_within_256mib": max(
                full["peak_cache_bytes"],
                progressive["final_cache_bytes"],
            )
            <= DEFAULT_RENDER_CACHE_BYTES,
            "actual_dense_4096_ten_layers": (
                dense_tiles == LAYER_COUNT * TILES_PER_AXIS * TILES_PER_AXIS
                and dense_bytes == LAYER_COUNT * DIMENSION * DIMENSION * 4
            ),
        },
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"P2 benchmark results: {OUTPUT_PATH.resolve()}")
    return 0 if all(results["acceptance"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
