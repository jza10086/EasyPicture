from __future__ import annotations

import base64
import json
import re
import zipfile
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    RasterGroupLayer,
    SvgElementObject,
    TextObject,
    VectorGroupLayer,
)
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    PROJECT_SCHEMA_VERSION,
    DocumentSnapshot,
    ImageExportRequest,
    SvgExportOptions,
    SvgVectorExportMode,
    VectorGroupSnapshot,
    export_image,
    import_svg,
    load_project,
    save_project,
)


def _progress(_done: int, _total: int, _message: str) -> None:
    pass


def _write_svg(path: Path) -> None:
    path.write_text(
        """
        <svg xmlns="http://www.w3.org/2000/svg" width="96" height="64">
          <defs>
            <linearGradient id="gradient"><stop stop-color="#f00"/></linearGradient>
          </defs>
          <rect id="box" x="4" y="5" width="30" height="20" fill="url(#gradient)"/>
          <text id="label" x="8" y="50" font-size="14">SVG 文字</text>
          <script>alert('blocked')</script>
          <image href="https://example.invalid/a.png"/>
        </svg>
        """,
        encoding="utf-8",
    )


def _render_svg(path: Path, width: int, height: int) -> np.ndarray:
    renderer = QSvgRenderer(QByteArray(path.read_bytes()))
    assert renderer.isValid()
    image = QImage(width, height, QImage.Format.Format_RGBA8888)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0.0, 0.0, float(width), float(height)))
    painter.end()
    return np.frombuffer(image.bits(), dtype=np.uint8).reshape(height, width, 4).copy()


def test_static_svg_import_creates_one_vector_child_per_leaf(
    tmp_path: Path,
    qapp,
) -> None:
    _ = qapp
    source = tmp_path / "sample.svg"
    _write_svg(source)
    result = import_svg(source, CancellationToken(), _progress)
    assert result.imported_count == 2
    assert result.warnings
    assert result.document.source_name == "sample.svg"
    assert not result.document.modified
    group = next(node for node in result.document.nodes if isinstance(node, VectorGroupLayer))
    assert len(group.children) == 2
    assert isinstance(group.children[0], SvgElementObject)
    assert isinstance(group.children[1], TextObject)


def test_complex_svg_text_is_preserved_as_svg_object(tmp_path: Path, qapp) -> None:
    _ = qapp
    source = tmp_path / "complex-text.svg"
    source.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="96" height="64">
        <text id="complex" x="4" y="20"><tspan dx="2">A</tspan><tspan>B</tspan></text>
        </svg>""",
        encoding="utf-8",
    )
    result = import_svg(source, CancellationToken(), _progress)
    group = next(node for node in result.document.nodes if isinstance(node, VectorGroupLayer))
    assert isinstance(group.children[0], SvgElementObject)
    assert group.children[0].element_kind == "text"
    assert any(warning.category == "复杂文字" for warning in result.warnings)


def test_import_warns_and_removes_unsupported_css_selectors(tmp_path: Path, qapp) -> None:
    _ = qapp
    source = tmp_path / "css.svg"
    source.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">
        <style>rect { fill:#ff0000 } g > rect { stroke:#00ff00 }</style>
        <rect id="box" x="2" y="2" width="20" height="20"/>
        </svg>""",
        encoding="utf-8",
    )
    result = import_svg(source, CancellationToken(), _progress)
    assert result.imported_count == 1
    assert any(item.category == "不支持 CSS" for item in result.warnings)
    vector = result.document.vector_groups[0].children[0]
    assert isinstance(vector, SvgElementObject)
    assert "g &gt; rect" not in vector.svg


def test_svg_objects_round_trip_in_current_schema(tmp_path: Path, qapp) -> None:
    _ = qapp
    source = tmp_path / "sample.svg"
    _write_svg(source)
    imported = import_svg(source, CancellationToken(), _progress)
    project = tmp_path / "sample.limg"
    save_project(
        DocumentSnapshot.capture(imported.document),
        project,
        CancellationToken(),
        _progress,
    )
    with zipfile.ZipFile(project) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == PROJECT_SCHEMA_VERSION
        assert any(name.startswith("vectors/") for name in archive.namelist())
    loaded = load_project(project, CancellationToken(), _progress)
    vector = next(node for node in loaded.snapshot.nodes if isinstance(node, VectorGroupSnapshot))
    assert len(vector.children) == 2


def test_svg_import_rejects_dtd_and_entities(tmp_path: Path, qapp) -> None:
    _ = qapp
    source = tmp_path / "unsafe.svg"
    source.write_text(
        """<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
        <svg xmlns="http://www.w3.org/2000/svg"><text>&xxe;</text></svg>""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="DTD|实体"):
        import_svg(source, CancellationToken(), _progress)


def test_svg_import_skips_external_resource_but_keeps_safe_leaf(
    tmp_path: Path,
    qapp,
) -> None:
    _ = qapp
    source = tmp_path / "partial.svg"
    source.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
        <rect x="2" y="2" width="20" height="20" fill="#ff6600"/>
        <use href="https://example.invalid/icon.svg#part"/>
        </svg>""",
        encoding="utf-8",
    )

    result = import_svg(source, CancellationToken(), _progress)
    group = next(node for node in result.document.nodes if isinstance(node, VectorGroupLayer))
    assert len(group.children) == 1
    assert isinstance(group.children[0], SvgElementObject)
    assert any("外部" in warning.message for warning in result.warnings)


def test_svg_export_preserves_or_rasterizes_vectors(tmp_path: Path, qapp) -> None:
    _ = qapp
    source = tmp_path / "sample.svg"
    _write_svg(source)
    document = import_svg(source, CancellationToken(), _progress).document
    snapshot = DocumentSnapshot.capture(document)
    preserved = export_image(
        ImageExportRequest(snapshot, tmp_path / "preserved.svg", SvgExportOptions()),
        CancellationToken(),
        _progress,
    )
    rasterized = export_image(
        ImageExportRequest(
            snapshot,
            tmp_path / "rasterized.svg",
            SvgExportOptions(SvgVectorExportMode.RASTERIZE_VECTOR),
        ),
        CancellationToken(),
        _progress,
    )
    preserve_text = preserved.path.read_text(encoding="utf-8")
    raster_text = rasterized.path.read_text(encoding="utf-8")
    assert "data:image/png;base64" not in preserve_text
    assert preserve_text.count("<svg") == 1
    assert "ns0:" not in preserve_text
    assert "EasyPicture" not in preserve_text
    assert "group-" not in preserve_text
    assert "object-" not in preserve_text
    assert "ep-" not in preserve_text
    assert (
        re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            preserve_text,
        )
        is None
    )
    assert "<text" in preserve_text
    assert raster_text.count("<image") == 1
    assert raster_text.count("data:image/png;base64") == 1
    assert "<text" not in raster_text


def test_svg_export_is_flat_pixel_equivalent_and_reimportable(
    tmp_path: Path,
    qapp,
) -> None:
    _ = qapp
    source = tmp_path / "icon.svg"
    source.write_text(
        """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128">
        <rect width="124" height="124" x="2" y="2" fill="#363d52"
              stroke="#212532" stroke-width="4" rx="14"/>
        <g fill="#fff" transform="translate(12 12) scale(.1)">
          <path d="M100 670v35q410 350 820 0v-35z"/>
          <circle cx="300" cy="525" r="90"/>
        </g></svg>""",
        encoding="utf-8",
    )
    imported = import_svg(source, CancellationToken(), _progress)
    destination = tmp_path / "exported.svg"
    export_image(
        ImageExportRequest(
            DocumentSnapshot.capture(imported.document),
            destination,
            SvgExportOptions(),
        ),
        CancellationToken(),
        _progress,
    )

    exported_text = destination.read_text(encoding="utf-8")
    assert exported_text.count("<svg") == 1
    assert "ns0:" not in exported_text
    assert "data:image/png;base64" not in exported_text
    assert import_svg(destination, CancellationToken(), _progress).imported_count == 3
    assert np.array_equal(_render_svg(source, 128, 128), _render_svg(destination, 128, 128))


def test_svg_import_flattens_standard_nested_viewport_and_skips_empty_png(
    tmp_path: Path,
    qapp,
) -> None:
    _ = qapp
    buffer = BytesIO()
    Image.new("RGBA", (128, 128), (0, 0, 0, 0)).save(buffer, format="PNG")
    transparent_png = base64.b64encode(buffer.getvalue()).decode("ascii")
    source = tmp_path / "nested.svg"
    source.write_text(
        f"""<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128"
        viewBox="0 0 128 128">
        <image width="128" height="128" href="data:image/png;base64,{transparent_png}"/>
        <g><ns0:svg xmlns:ns0="http://www.w3.org/2000/svg" width="128"
        height="128" viewBox="0 0 128 128">
        <ns0:rect id="nested-box" x="8" y="8" width="40" height="30" fill="#f60"/>
        </ns0:svg></g></svg>""",
        encoding="utf-8",
    )

    result = import_svg(source, CancellationToken(), _progress)
    assert result.imported_count == 1
    assert any(warning.category == "嵌套视口转换" for warning in result.warnings)
    assert any("完全透明" in warning.message for warning in result.warnings)


def test_vector_child_rasterization_splits_group_and_undoes(
    tmp_path: Path,
    qapp,
) -> None:
    _ = qapp
    source = tmp_path / "sample.svg"
    _write_svg(source)
    document = import_svg(source, CancellationToken(), _progress).document
    service = DocumentCommandService()
    service.attach_document(document)
    group = next(node for node in document.nodes if isinstance(node, VectorGroupLayer))
    target = group.children[0]
    assert service.rasterize_vector_object(target.id)
    assert sum(isinstance(node, RasterGroupLayer) for node in document.nodes) == 2
    assert service.undo_stack.count() == 1
    service.undo_stack.undo()
    restored = next(node for node in document.nodes if isinstance(node, VectorGroupLayer))
    assert [child.id for child in restored.children] == [child.id for child in group.children]
