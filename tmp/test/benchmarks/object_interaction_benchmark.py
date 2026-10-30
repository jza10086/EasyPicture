"""Offscreen input, outliner and bounded color-preview measurements."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import numpy as np  # noqa: E402
import psutil  # noqa: E402
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QImage, QMouseEvent, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from easypicture.commands import DocumentCommandService  # noqa: E402
from easypicture.diagnostics import performance_counters  # noqa: E402
from easypicture.domain import (  # noqa: E402
    Document,
    SvgPrimitiveKind,
    SvgPrimitiveStyle,
    SvgRectGeometry,
    create_svg_primitive,
)
from easypicture.imaging import ColorAdjustmentRequest, InvertAdjustment  # noqa: E402
from easypicture.imaging.color_preview import calculate_color_preview  # noqa: E402
from easypicture.jobs import CancellationToken  # noqa: E402
from easypicture.persistence import DocumentSnapshot  # noqa: E402
from easypicture.render import CanvasRenderCoordinator  # noqa: E402
from easypicture.ui.layer_panel import LayerPanel  # noqa: E402
from easypicture.ui.object_properties import ObjectTransformBlock  # noqa: E402
from easypicture.ui.pointer_backend import (  # noqa: E402
    VirtualPointerBackend,
    configure_pointer_backend,
)


def statistics(values):
    return {
        "p50_ms": round(float(np.percentile(values, 50)), 3),
        "p95_ms": round(float(np.percentile(values, 95)), 3),
        "samples": len(values),
    }


def input_scene(app, size, count):
    document = Document.create(size, size)
    commands = DocumentCommandService()
    commands.attach_document(document)
    for index in range(count):
        node = create_svg_primitive(
            SvgPrimitiveKind.RECT,
            SvgRectGeometry(index % 32 * 30, index // 32 * 30, 20, 20),
            SvgPrimitiveStyle(fill=(220, 40, 20, 255)),
            document.size,
        )
        document.insert_vector_object(document.nodes[0].id, node, index)
    panel = LayerPanel(commands)
    panel.set_document(document)
    unsubscribe = document.subscribe(panel.handle_event)
    block = ObjectTransformBlock(commands, lambda: document, panel.selection)
    renderer = CanvasRenderCoordinator()
    renderer.set_document(document)
    block.preview_changed.connect(renderer.set_object_transform_preview)
    block.preview_cleared.connect(renderer.clear_object_transform_preview)
    panel.select_node(document.nodes[0].children[0])
    block.resize(300, 500)
    block.show()
    app.processEvents()
    field = block.fields["rotation"]._field
    local = QPointF(field.rect().center())
    origin = QPointF(field.mapToGlobal(local.toPoint()))
    QApplication.sendEvent(
        field,
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            local,
            origin,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    inputs, latencies, selections, renames = [], [], [], []
    image = QImage(1024, 1024, QImage.Format.Format_RGBA8888_Premultiplied)
    performance_counters.reset()
    for index in range(22):
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            local + QPointF(index + 1, 0),
            origin + QPointF(index + 1, 0),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        start = time.perf_counter_ns()
        QApplication.sendEvent(field, event)
        elapsed = (time.perf_counter_ns() - start) / 1e6
        image.fill(0)
        painter = QPainter(image)
        painter.scale(1024 / size, 1024 / size)
        renderer.paint_software(painter, QRectF(0, 0, size, size))
        painter.end()
        if index >= 2:
            inputs.append(elapsed)
            latencies.append((time.perf_counter_ns() - start) / 1e6)
    QApplication.sendEvent(
        field,
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            local,
            origin,
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    assert commands.undo_stack.count() == 1
    children = document.nodes[0].children
    for index in range(22):
        node = children[index % len(children)]
        start = time.perf_counter_ns()
        panel.select_node(node)
        if index >= 2:
            selections.append((time.perf_counter_ns() - start) / 1e6)
        start = time.perf_counter_ns()
        assert panel.tree_model.setData(panel.tree_model.index_for_id(node.id), f"Object {index}")
        if index >= 2:
            renames.append((time.perf_counter_ns() - start) / 1e6)
    result = {
        "size": size,
        "vectors": count,
        "input": statistics(inputs),
        "input_to_software_preview": statistics(latencies),
        "selection": statistics(selections),
        "rename": statistics(renames),
        "counters": performance_counters.snapshot(),
        "sampled_rss_bytes": psutil.Process().memory_info().rss,
        "os_process_peak_working_set_bytes": getattr(
            psutil.Process().memory_info(), "peak_wset", None
        ),
    }
    unsubscribe()
    block.close()
    panel.close()
    renderer.dispose()
    commands.attach_document(None)
    return result


def color_scene():
    document = Document.create(4096, 4096)
    tile = np.full((256, 256, 4), (50, 80, 110, 255), np.uint8)
    for y in range(16):
        for x in range(16):
            document.active_layer.pixels.replace_tile(x, y, tile)
    request = ColorAdjustmentRequest(
        DocumentSnapshot.capture(document), document.active_layer_id, InvertAdjustment()
    )
    values = []
    peak = 0
    for index in range(22):
        start = time.perf_counter_ns()
        result = calculate_color_preview(request, CancellationToken(), lambda *_: None)
        assert result.preview_size.width == 1024
        if index >= 2:
            values.append((time.perf_counter_ns() - start) / 1e6)
        peak = max(peak, psutil.Process().memory_info().rss)
    return {
        "cpu_preview": statistics(values),
        "sampled_rss_bytes": peak,
        "os_process_peak_working_set_bytes": getattr(
            psutil.Process().memory_info(), "peak_wset", None
        ),
    }


def main():
    app = QApplication([])
    configure_pointer_backend(VirtualPointerBackend())
    scenarios = []
    for size, count in ((128, 0), (2048, 20), (1024, 100), (1024, 1000)):
        result = input_scene(app, size, count)
        scenarios.append(result)
        print(json.dumps(result), flush=True)
    color = color_scene()
    print(json.dumps(color), flush=True)
    output = Path(__file__).resolve().parents[1] / "runs" / "object-refactor-interaction.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "backend": "offscreen virtual pointer + QPainter",
                "warmups": 2,
                "scenarios": scenarios,
                "4096_color": color,
                "limits": "Input processing and synchronous software paint; color CPU excludes "
                "queue wait and display. RSS sampled; Windows peak working set is a process "
                "high-water mark. Native input is unmeasured.",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
