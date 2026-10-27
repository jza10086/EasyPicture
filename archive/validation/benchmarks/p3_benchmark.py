"""P3 dense project save/load, snapshot, memory, and cancellation benchmark."""

from __future__ import annotations

import gc
import json
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil

from easypicture.domain import Document, RasterObject
from easypicture.jobs import CancellationToken, JobCancelled
from easypicture.persistence import DocumentSnapshot, load_project, save_project

OUTPUT_PATH = (
    Path(os.environ.get("EASYPICTURE_BENCHMARK_DIR", "../archive/runs/benchmarks")) / "p3.json"
)
PROJECT_PATH = (
    Path(os.environ.get("EASYPICTURE_BENCHMARK_DIR", "../archive/runs/benchmarks"))
    / "p3-dense.limg"
)
CANCEL_PATH = (
    Path(os.environ.get("EASYPICTURE_BENCHMARK_DIR", "../archive/runs/benchmarks"))
    / "p3-cancelled.limg"
)
DIMENSION = 4096
LAYER_COUNT = 10
TILES_PER_AXIS = DIMENSION // 256


def progress(_completed: int, _total: int, _message: str) -> None:
    return


def build_dense_document() -> Document:
    document = Document.create(DIMENSION, DIMENSION)
    for index in range(1, LAYER_COUNT):
        document.insert_layer(
            RasterObject.create_blank(f"基准图层 {index + 1}", document.size),
            index,
        )
    for layer_index, layer in enumerate(document.layers):
        color = (
            (layer_index * 31 + 17) % 256,
            (layer_index * 47 + 29) % 256,
            (layer_index * 67 + 43) % 256,
            64 + layer_index * 15,
        )
        for ty in range(TILES_PER_AXIS):
            for tx in range(TILES_PER_AXIS):
                layer.pixels.writable_tile(tx, ty)[:] = color
    document.mark_clean()
    return document


def measure(operation):
    process = psutil.Process()
    peak = process.memory_info().rss
    stop = threading.Event()

    def sample() -> None:
        nonlocal peak
        while not stop.wait(0.01):
            peak = max(peak, process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter_ns()
    try:
        value = operation()
    finally:
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        stop.set()
        sampler.join()
        peak = max(peak, process.memory_info().rss)
    return value, elapsed_ms, peak


def cancellation_latency(snapshot: DocumentSnapshot) -> float:
    token = CancellationToken()
    result: list[BaseException | None] = []

    def save() -> None:
        try:
            save_project(snapshot, CANCEL_PATH, token, progress)
            result.append(None)
        except BaseException as error:
            result.append(error)

    worker = threading.Thread(target=save)
    worker.start()
    time.sleep(0.05)
    started = time.perf_counter_ns()
    token.cancel()
    worker.join(timeout=5)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if worker.is_alive() or not result or not isinstance(result[0], JobCancelled):
        raise AssertionError("P3 save did not cooperatively cancel")
    if CANCEL_PATH.exists():
        raise AssertionError("cancelled save left a destination project")
    return elapsed_ms


def main() -> int:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROJECT_PATH.unlink(missing_ok=True)
    CANCEL_PATH.unlink(missing_ok=True)
    process = psutil.Process()
    gc.collect()
    rss_before = process.memory_info().rss
    document, allocation_ms, allocation_peak = measure(build_dense_document)
    dense_bytes = sum(layer.pixels.allocated_bytes for layer in document.layers)
    dense_tiles = sum(layer.pixels.allocated_tile_count for layer in document.layers)

    started = time.perf_counter_ns()
    snapshot = DocumentSnapshot.capture(document)
    snapshot_ms = (time.perf_counter_ns() - started) / 1_000_000
    _receipt, save_ms, save_peak = measure(
        lambda: save_project(snapshot, PROJECT_PATH, CancellationToken(), progress)
    )
    loaded, load_ms, load_peak = measure(
        lambda: load_project(PROJECT_PATH, CancellationToken(), progress)
    )
    cancel_ms = cancellation_latency(snapshot)
    sample = loaded.snapshot.layers[-1].pixels.read_tile(0, 0)[0, 0]
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
            "project_bytes": PROJECT_PATH.stat().st_size,
        },
        "snapshot_ms": round(snapshot_ms, 3),
        "save_ms": round(save_ms, 3),
        "load_ms": round(load_ms, 3),
        "cancel_latency_ms": round(cancel_ms, 3),
        "rss": {
            "before": rss_before,
            "allocation_peak": allocation_peak,
            "save_peak": save_peak,
            "load_peak": load_peak,
        },
        "sample_pixel": [int(value) for value in sample],
        "acceptance": {
            "snapshot_under_100ms": snapshot_ms < 100.0,
            "cancel_under_1000ms": cancel_ms < 1000.0,
            "actual_dense_4096_ten_layers": (
                dense_tiles == LAYER_COUNT * TILES_PER_AXIS * TILES_PER_AXIS
                and dense_bytes == LAYER_COUNT * DIMENSION * DIMENSION * 4
            ),
        },
    }
    OUTPUT_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"P3 benchmark results: {OUTPUT_PATH.resolve()}")
    return 0 if all(results["acceptance"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
