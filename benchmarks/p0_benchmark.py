"""Repeatable P0 Tile, memory, QImage bridge, and alpha-composite benchmark."""

from __future__ import annotations

import gc
import json
import math
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
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColor, QImage, QPainter

from easypicture.pixels import Rect, TilePixelStore
from easypicture.render import rgba_tile_to_qimage

OUTPUT_PATH = Path("build/benchmarks/p0.json")
TILE_SIZES = (256, 512)


def elapsed_ms(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000


def percentile_95(values: list[float]) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), 95))


def benchmark_local_edits(tile_size: int, region_size: int, iterations: int = 30) -> float:
    store = TilePixelStore(tile_size)
    pixels = np.full((region_size, region_size, 4), (41, 92, 173, 191), dtype=np.uint8)
    samples: list[float] = []
    for index in range(iterations):
        offset = (index * 37) % tile_size
        rect = Rect(offset, offset, region_size, region_size)
        start = time.perf_counter_ns()
        store.write_region(rect, pixels)
        samples.append(elapsed_ms(start))
    return percentile_95(samples)


def create_dense_store(tile_size: int, dimension: int) -> tuple[TilePixelStore, float]:
    store = TilePixelStore(tile_size)
    tile_count = math.ceil(dimension / tile_size)
    start = time.perf_counter_ns()
    for ty in range(tile_count):
        for tx in range(tile_count):
            tile = store.writable_tile(tx, ty)
            tile[..., 0] = (tx * 17) % 256
            tile[..., 1] = (ty * 29) % 256
            tile[..., 2] = 127
            tile[..., 3] = 255
    return store, elapsed_ms(start)


def benchmark_traversal(store: TilePixelStore, repeats: int = 3) -> float:
    samples: list[float] = []
    checksum = 0
    for _repeat in range(repeats):
        start = time.perf_counter_ns()
        for _coord, tile in store.iter_allocated_tiles():
            checksum ^= int(tile.sum(dtype=np.uint64))
        samples.append(elapsed_ms(start))
    if checksum < 0:  # pragma: no cover - keeps the traversal observable
        raise AssertionError("unreachable checksum")
    return statistics.median(samples)


def benchmark_dense_case(tile_size: int, dimension: int) -> dict[str, float | int]:
    process = psutil.Process()
    gc.collect()
    rss_before = process.memory_info().rss
    store, allocation_ms = create_dense_store(tile_size, dimension)
    rss_after = process.memory_info().rss
    traversal_ms = benchmark_traversal(store, repeats=3 if dimension == 4096 else 1)
    result: dict[str, float | int] = {
        "allocated_bytes": store.allocated_bytes,
        "allocated_tiles": store.allocated_tile_count,
        "allocation_ms": round(allocation_ms, 3),
        "rss_delta_bytes": max(0, rss_after - rss_before),
        "traversal_ms": round(traversal_ms, 3),
    }
    del store
    gc.collect()
    return result


def benchmark_qimage_bridge(tile_size: int, iterations: int = 60) -> dict[str, float]:
    rng = np.random.default_rng(20260723)
    tile = rng.integers(0, 256, size=(tile_size, tile_size, 4), dtype=np.uint8)
    samples: list[float] = []
    for _iteration in range(iterations):
        start = time.perf_counter_ns()
        image = rgba_tile_to_qimage(tile)
        samples.append(elapsed_ms(start))
        if image.isNull():
            raise AssertionError("bridge returned a null image")
    return {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile_95(samples), 3),
    }


def reference_source_over(
    source: tuple[int, int, int, int],
    destination: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    src = np.asarray(source, dtype=np.float64) / 255.0
    dst = np.asarray(destination, dtype=np.float64) / 255.0
    out_alpha = src[3] + dst[3] * (1.0 - src[3])
    if out_alpha == 0:
        return (0, 0, 0, 0)
    out_rgb = (src[:3] * src[3] + dst[:3] * dst[3] * (1.0 - src[3])) / out_alpha
    out = np.concatenate((out_rgb, [out_alpha]))
    return tuple(int(round(value * 255.0)) for value in out)  # type: ignore[return-value]


def verify_alpha_composite() -> dict[str, Any]:
    source_rgba = (220, 30, 40, 128)
    destination_rgba = (20, 60, 210, 192)
    source = np.asarray([[source_rgba]], dtype=np.uint8)
    source_image = rgba_tile_to_qimage(source)
    destination = QImage(1, 1, QImage.Format.Format_RGBA8888_Premultiplied)
    destination.fill(QColor(*destination_rgba))
    painter = QPainter(destination)
    painter.drawImage(QPoint(0, 0), source_image)
    painter.end()

    actual_color = destination.pixelColor(0, 0)
    actual = (
        actual_color.red(),
        actual_color.green(),
        actual_color.blue(),
        actual_color.alpha(),
    )
    expected = reference_source_over(source_rgba, destination_rgba)
    max_error = max(abs(left - right) for left, right in zip(actual, expected, strict=True))
    if max_error > 1:
        raise AssertionError(
            f"alpha composite exceeded 1 LSB: actual={actual}, expected={expected}"
        )
    return {"actual": actual, "expected": expected, "max_error_lsb": max_error}


def choose_tile_size(results: dict[str, Any]) -> dict[str, Any]:
    result_256 = results["tiles"]["256"]
    result_512 = results["tiles"]["512"]
    traversal_256 = result_256["dense_4096"]["traversal_ms"]
    traversal_512 = result_512["dense_4096"]["traversal_ms"]
    local_256 = statistics.mean((result_256["local_128_p95_ms"], result_256["local_512_p95_ms"]))
    local_512 = statistics.mean((result_512["local_128_p95_ms"], result_512["local_512_p95_ms"]))
    traversal_pass = traversal_512 <= traversal_256 * 0.80
    local_pass = local_512 <= local_256 * 1.10
    selected = 512 if traversal_pass and local_pass else 256
    return {
        "selected": selected,
        "rule": (
            "Select 512 only when 4096 traversal is at least 20% faster and "
            "mean local-edit P95 is no more than 10% slower."
        ),
        "traversal_pass": traversal_pass,
        "local_edit_pass": local_pass,
        "traversal_ratio_512_to_256": round(traversal_512 / traversal_256, 4),
        "local_ratio_512_to_256": round(local_512 / local_256, 4),
    }


def main() -> int:
    results: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": {
            "python": sys.version,
            "system": platform.platform(),
            "processor": platform.processor(),
            "physical_memory_bytes": psutil.virtual_memory().total,
        },
        "tiles": {},
    }

    for tile_size in TILE_SIZES:
        print(f"Benchmarking tile size {tile_size}...", flush=True)
        results["tiles"][str(tile_size)] = {
            "local_128_p95_ms": round(benchmark_local_edits(tile_size, 128), 3),
            "local_512_p95_ms": round(benchmark_local_edits(tile_size, 512), 3),
            "qimage_bridge": benchmark_qimage_bridge(tile_size),
            "dense_4096": benchmark_dense_case(tile_size, 4096),
            "dense_8192": benchmark_dense_case(tile_size, 8192),
        }

    results["alpha_composite"] = verify_alpha_composite()
    results["decision"] = choose_tile_size(results)
    p95_256 = results["tiles"]["256"]["qimage_bridge"]["p95_ms"]
    results["acceptance"] = {
        "qimage_bridge_256_p95_under_5ms": p95_256 <= 5.0,
        "alpha_composite_within_1_lsb": results["alpha_composite"]["max_error_lsb"] <= 1,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results["decision"], indent=2))
    print(f"Benchmark results: {OUTPUT_PATH.resolve()}")
    return 0 if all(results["acceptance"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
