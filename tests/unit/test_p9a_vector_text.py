from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import QScrollArea

from easypicture.commands import DocumentCommandService
from easypicture.domain import (
    AffineTransform,
    Document,
    FloatRect,
    LayerTreeChanged,
    Point,
    TextHorizontalAlignment,
    TextWrapMode,
)
from easypicture.imaging import compose_document_tile
from easypicture.jobs import CancellationToken
from easypicture.persistence import DocumentSnapshot
from easypicture.persistence.project_io import load_project, save_project
from easypicture.pixels import TileCoord
from easypicture.tools.text_tool import TextTool, TextTransformTool
from easypicture.ui.layer_panel import LayerTreeModel
from easypicture.ui.text_properties import TextPropertiesPanel


def _progress(_completed: int, _total: int, _message: str) -> None:
    return


def _text_document() -> tuple[Document, DocumentCommandService]:
    document = Document.create(320, 200)
    commands = DocumentCommandService()
    commands.attach_document(document)
    assert commands.create_text_group("Labels")
    group = document.text_groups[0]
    assert commands.create_text_object(
        group.id,
        FloatRect(16.0, 12.0, 240.0, 96.0),
        name="Title",
        text="你好\nEasyPicture",
    )
    return document, commands


def test_text_commands_are_undoable_and_keep_raster_compatibility() -> None:
    document, commands = _text_document()
    group = document.text_groups[0]
    text_object = group.children[0]
    assert document.layers == (document.active_layer,)
    assert commands.update_text_object(
        text_object.id,
        horizontal_alignment=TextHorizontalAlignment.CENTER,
        opacity=0.5,
    )
    assert text_object.horizontal_alignment is TextHorizontalAlignment.CENTER
    assert text_object.opacity == 0.5
    commands.undo_stack.undo()
    assert text_object.opacity == 1.0
    commands.undo_stack.redo()
    assert text_object.opacity == 0.5


def test_text_is_included_in_authoritative_cpu_composition(qapp) -> None:
    _ = qapp
    document, _commands = _text_document()
    snapshot = DocumentSnapshot.capture(document)
    tile = compose_document_tile(snapshot, TileCoord(0, 0))
    assert tile.dtype == np.uint8
    assert tile.shape == (256, 256, 4)
    assert int(tile[..., 3].max()) > 0


def test_text_wrap_modes_use_character_wrap_or_explicit_breaks_only(qapp) -> None:
    _ = qapp
    document, commands = _text_document()
    text_object = document.text_groups[0].children[0]
    assert commands.update_text_object(
        text_object.id,
        text="ABCDEFGHIJK",
        frame=FloatRect(8.0, 8.0, 28.0, 160.0),
        font_size=24.0,
        wrap_mode=TextWrapMode.MANUAL,
    )
    manual = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))
    manual_rows = np.flatnonzero(np.any(manual[..., 3] > 0, axis=1))
    assert commands.update_text_object(text_object.id, wrap_mode=TextWrapMode.AUTO)
    automatic = compose_document_tile(DocumentSnapshot.capture(document), TileCoord(0, 0))
    automatic_rows = np.flatnonzero(np.any(automatic[..., 3] > 0, axis=1))
    assert automatic_rows[-1] > manual_rows[-1] + 20

    assert commands.update_text_object(
        text_object.id,
        text="A\nB",
        wrap_mode=TextWrapMode.MANUAL,
    )
    explicit_break = compose_document_tile(
        DocumentSnapshot.capture(document),
        TileCoord(0, 0),
    )
    explicit_rows = np.flatnonzero(np.any(explicit_break[..., 3] > 0, axis=1))
    assert explicit_rows[-1] > manual_rows[-1]


def test_limg_v2_round_trip_preserves_text_metadata(tmp_path: Path) -> None:
    document, commands = _text_document()
    original = document.text_groups[0].children[0]
    assert commands.update_text_object(
        original.id,
        stroke_color=(255, 64, 32, 192),
        stroke_width=3.5,
        transform_pivot=Point(120.0, 60.0),
        wrap_mode=TextWrapMode.MANUAL,
    )
    path = tmp_path / "unicode-text.limg"
    save_project(DocumentSnapshot.capture(document), path, CancellationToken(), _progress)
    loaded = load_project(path, CancellationToken(), _progress)
    restored = loaded.snapshot.to_document(project_path=path)
    assert len(restored.text_groups) == 1
    text_object = restored.text_groups[0].children[0]
    assert text_object.name == "Title"
    assert text_object.text == "你好\nEasyPicture"
    assert text_object.frame == FloatRect(16.0, 12.0, 240.0, 96.0)
    assert text_object.stroke_color == (255, 64, 32, 192)
    assert text_object.stroke_width == 3.5
    assert text_object.transform_pivot == Point(120.0, 60.0)
    assert text_object.wrap_mode is TextWrapMode.MANUAL


def test_limg_v1_raster_manifest_migrates_to_v2(tmp_path: Path) -> None:
    document = Document.create(64, 64)
    source = tmp_path / "source.limg"
    legacy = tmp_path / "legacy.limg"
    save_project(DocumentSnapshot.capture(document), source, CancellationToken(), _progress)
    with zipfile.ZipFile(source) as source_archive, zipfile.ZipFile(
        legacy,
        "w",
        compression=zipfile.ZIP_STORED,
    ) as legacy_archive:
        for info in source_archive.infolist():
            payload = source_archive.read(info)
            if info.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest["schema_version"] = 1
                manifest.pop("nodes", None)
                payload = json.dumps(manifest).encode("utf-8")
            legacy_archive.writestr(info.filename, payload)
    restored = load_project(legacy, CancellationToken(), _progress).snapshot.to_document()
    assert len(restored.layers) == 1
    assert restored.nodes[0].children[0].id == restored.layers[0].id


def test_early_limg_v2_without_stroke_fields_uses_disabled_default(
    tmp_path: Path,
) -> None:
    document, _commands = _text_document()
    source = tmp_path / "source-v2.limg"
    legacy = tmp_path / "early-v2.limg"
    save_project(DocumentSnapshot.capture(document), source, CancellationToken(), _progress)
    with zipfile.ZipFile(source) as source_archive, zipfile.ZipFile(
        legacy,
        "w",
        compression=zipfile.ZIP_STORED,
    ) as legacy_archive:
        for info in source_archive.infolist():
            payload = source_archive.read(info)
            if info.filename == "manifest.json":
                manifest = json.loads(payload)
                manifest["nodes"][-1]["children"][0].pop("stroke")
                manifest["nodes"][-1]["children"][0].pop("transform_pivot")
                manifest["nodes"][-1]["children"][0].pop("wrap_mode")
                payload = json.dumps(manifest).encode("utf-8")
            legacy_archive.writestr(info.filename, payload)
    restored = load_project(legacy, CancellationToken(), _progress).snapshot.to_document()
    text_object = restored.text_groups[0].children[0]
    assert text_object.stroke_width == 0.0
    assert text_object.stroke_color == (0, 0, 0, 255)
    assert text_object.transform_pivot is None
    assert text_object.wrap_mode is TextWrapMode.AUTO


def test_layer_tree_displays_top_level_and_children_indented(qapp) -> None:
    _ = qapp
    document, commands = _text_document()
    model = LayerTreeModel(commands)
    model.set_document(document)
    assert model.rowCount() == 2
    group_index = model.index(0, 0)
    assert model.data(group_index, Qt.ItemDataRole.DisplayRole) == "Labels"
    assert model.rowCount(group_index) == 1
    child_index = model.index(0, 0, group_index)
    assert model.data(child_index, Qt.ItemDataRole.DisplayRole) == "Title"
    assert model.parent(child_index) == group_index
    group_icon = model.data(group_index, Qt.ItemDataRole.DecorationRole)
    child_icon = model.data(child_index, Qt.ItemDataRole.DecorationRole)
    assert isinstance(group_icon, QIcon) and not group_icon.isNull()
    assert isinstance(child_icon, QIcon) and not child_icon.isNull()


def test_text_properties_use_the_dock_scroll_area_only(qapp) -> None:
    _ = qapp
    document, commands = _text_document()
    text_object = document.text_groups[0].children[0]
    panel = TextPropertiesPanel(commands)
    panel.set_object(text_object)
    panel.text_edit.setPlainText("属性栏文字")
    panel._set_stroke_color(QColor(12, 34, 56, 255))
    panel.stroke_width_spin.setValue(2.0)
    panel.wrap_combo.setCurrentIndex(panel.wrap_combo.findData("manual"))
    assert text_object.text == "属性栏文字"
    assert text_object.stroke_width == 2.0
    assert text_object.stroke_color == (12, 34, 56, 255)
    assert text_object.wrap_mode is TextWrapMode.MANUAL
    assert panel.minimumSizeHint().height() > 300
    assert panel.findChildren(QScrollArea) == []


def test_text_names_are_unique_with_numeric_suffixes() -> None:
    document, commands = _text_document()
    raster = document.active_layer
    assert commands.rename_layer(raster.id, "Labels")
    assert raster.name == "Labels.1"
    assert commands.create_text_group("Labels")
    assert document.text_groups[-1].name == "Labels.2"
    group = document.text_groups[0]
    assert commands.create_text_object(group.id, name="Title")
    assert [child.name for child in group.children] == ["Title", "Title.1"]


def test_text_property_change_does_not_report_structural_tree_reset() -> None:
    document, commands = _text_document()
    text_object = document.text_groups[0].children[0]
    events = []
    document.subscribe(events.append)
    assert commands.update_text_object(text_object.id, font_size=48.0)
    assert not any(isinstance(event, LayerTreeChanged) for event in events)


def test_text_creation_uses_selected_group_or_creates_one(qapp) -> None:
    _ = qapp
    document = Document.create(320, 200)
    commands = DocumentCommandService()
    commands.attach_document(document)
    selected: list[object] = []
    overlays = SimpleNamespace(clear=lambda: None)
    context = SimpleNamespace(
        commands=commands,
        document_provider=lambda: document,
        selected_node_provider=lambda: (
            selected[-1] if selected else document.active_layer
        ),
        select_node=selected.append,
        overlays=overlays,
    )
    tool = TextTool()
    tool._context = context  # type: ignore[assignment]
    tool._create_text_object(QRectF(10.0, 20.0, 120.0, 48.0))
    assert len(document.text_groups) == 1
    group = document.text_groups[0]
    assert len(group.children) == 1
    selected[:] = [group]
    tool._create_text_object(QRectF(30.0, 40.0, 100.0, 40.0))
    assert len(document.text_groups) == 1
    assert len(group.children) == 2
    selected[:] = [group.children[-1]]
    selected_name = group.children[-1].name
    tool._create_text_object(QRectF(50.0, 60.0, 80.0, 32.0))
    assert len(group.children) == 3
    assert group.children[-1].name == selected_name
    assert group.children[-2] is selected[-1]


def test_text_transform_preview_keeps_unselected_siblings(qapp) -> None:
    _ = qapp
    document, commands = _text_document()
    group = document.text_groups[0]
    assert commands.create_text_object(group.id, name="Subtitle")
    selected = group.children[0]
    previews: list[tuple[object, ...]] = []
    canvas = SimpleNamespace(
        zoom=1.0,
        replace_vector_preview=lambda _group_id, objects: previews.append(tuple(objects)),
        clear_vector_preview=lambda: None,
    )
    overlays = SimpleNamespace(add=lambda _item: None, clear=lambda: None)
    context = SimpleNamespace(
        canvas=canvas,
        overlays=overlays,
        document_provider=lambda: document,
        status=lambda _message: None,
    )
    tool = TextTransformTool(lambda: selected)
    tool._context = context  # type: ignore[assignment]
    tool._begin_session()
    assert previews
    assert [item.id for item in previews[-1]] == [item.id for item in group.children]


def test_text_rotation_keeps_configured_center_fixed() -> None:
    pivot = Point(50.0, 25.0)
    transform = TextPropertiesPanel._transform_with_rotation(
        AffineTransform(),
        90.0,
        pivot,
    )
    mapped = transform.map_point(pivot)
    assert abs(mapped.x - pivot.x) < 1e-8
    assert abs(mapped.y - pivot.y) < 1e-8
