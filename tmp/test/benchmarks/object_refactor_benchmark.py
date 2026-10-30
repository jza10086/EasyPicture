"""Reproducible software-render baseline; no system pointer or resource digests."""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import sys
import time
from dataclasses import asdict
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import numpy as np  # noqa: E402
import psutil  # noqa: E402
from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from easypicture.commands import DocumentCommandService  # noqa: E402
from easypicture.diagnostics import performance_counters  # noqa: E402
from easypicture.domain import (  # noqa: E402
    Document,
    RasterObject,
    Size,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    create_svg_primitive,
)
from easypicture.render import CanvasRenderCoordinator  # noqa: E402
from easypicture.render.svg_renderer import svg_content_cache  # noqa: E402
from easypicture.render.text_renderer import text_content_cache  # noqa: E402


def measure(size: int, layers: int, vectors: int) -> dict[str, object]:
    svg_before = svg_content_cache().statistics
    text_before = text_content_cache().statistics
    document = Document.create(size, size)
    group = document.nodes[0]
    for index in range(1, layers):
        document.insert_layer(RasterObject.create_blank(f"Raster {index}", document.size), index)
    tile = np.full((256, 256, 4), (60, 90, 150, 128), dtype=np.uint8)
    for layer in group.children:
        for y in range((size + 255) // 256):
            for x in range((size + 255) // 256):
                layer.pixels.replace_tile(x, y, tile)
    commands = DocumentCommandService()
    commands.attach_document(document)
    if vectors:
        commands.create_vector_group()
        vector_group = document.vector_groups[0]
        for index in range(vectors):
            vector = create_svg_primitive(
                SvgPrimitiveKind.RECT,
                SvgRectGeometry(float(index % 32 * 30), float(index // 32 * 30), 20, 20),
                SvgPrimitiveStyle(fill=(220, 40, 20, 255)),
                Size(size, size),
                name=f"Vector {index}",
            )
            document.insert_vector_object(vector_group.id, vector, index)
    coordinator = CanvasRenderCoordinator()
    coordinator.set_document(document)
    image = QImage(size, size, QImage.Format.Format_RGBA8888_Premultiplied)
    durations = []
    peak_rss = 0
    process = psutil.Process()
    performance_counters.reset()
    for iteration in range(22):
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        start = time.perf_counter_ns()
        coordinator.paint_software(painter, QRectF(0, 0, size, size))
        painter.end()
        elapsed = (time.perf_counter_ns() - start) / 1_000_000
        if iteration >= 2:
            durations.append(elapsed)
        peak_rss = max(peak_rss, process.memory_info().rss)
    result = {
        "canvas": [size, size],
        "raster_layers": layers,
        "vector_objects": vectors,
        "warmups": 2,
        "samples": len(durations),
        "render_p50_ms": round(float(np.percentile(durations, 50)), 3),
        "render_p95_ms": round(float(np.percentile(durations, 95)), 3),
        "sampled_process_peak_rss_bytes": peak_rss,
        "os_process_peak_working_set_bytes": getattr(process.memory_info(), "peak_wset", None),
        "counters": performance_counters.snapshot(),
        "source_cache_bytes": coordinator.cache_bytes,
        "svg_cache": asdict(svg_content_cache().statistics),
        "svg_hits_this_scene": svg_content_cache().statistics.hits - svg_before.hits,
        "svg_misses_this_scene": svg_content_cache().statistics.misses - svg_before.misses,
        "text_hits_this_scene": text_content_cache().statistics.hits - text_before.hits,
        "text_misses_this_scene": text_content_cache().statistics.misses - text_before.misses,
    }
    coordinator.dispose()
    commands.attach_document(None)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=(Path(__file__).resolve().parents[1] / "runs" / "object-refactor-current.json"),
    )
    args = parser.parse_args()
    app = QApplication([])
    results = []
    for size, layers, vectors in (
        (128, 4, 0),
        (2048, 8, 20),
        (4096, 1, 0),
        (1024, 1, 100),
        (1024, 1, 1000),
    ):
        result = measure(size, layers, vectors)
        results.append(result)
        print(json.dumps(result), flush=True)
        app.processEvents()
        gc.collect()
    report = {
        "stage": "unified objects, bounded caches and completed preview scheduling",
        "python": sys.version,
        "platform": platform.platform(),
        "backend": "Qt QPainter / offscreen",
        "limitations": (
            "Rendering only; not native input latency, OpenGL, algorithm preview latency, "
            "or a pre-change speed comparison. RSS is sampled; Windows peak working set is "
            "a process-lifetime high-water mark, not reset between scenes."
        ),
        "scenarios": results,
    }
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
