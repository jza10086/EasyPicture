from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QDialog, QMessageBox

from easypicture.persistence import (
    ExportCapability,
    ExportFormat,
    GifExportOptions,
)
from easypicture.ui.export_dialog import ImageExportDialog
from tests.fakes import make_settings


def _capabilities(
    unavailable: ExportFormat | None = None,
) -> tuple[ExportCapability, ...]:
    return tuple(
        ExportCapability(
            export_format,
            export_format is not unavailable,
            "测试不可用" if export_format is unavailable else None,
        )
        for export_format in ExportFormat
    )


def test_export_dialog_switches_format_and_normalizes_extension(
    qtbot,
    tmp_path: Path,
) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    dialog = ImageExportDialog(
        settings,
        "sample.limg",
        tmp_path,
        _capabilities(),
    )
    qtbot.addWidget(dialog)

    assert dialog.export_format is ExportFormat.PNG
    assert dialog.path_edit.text().endswith("sample.png")
    dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(ExportFormat.GIF.value))
    dialog.gif_colors.setValue(32)
    dialog.gif_alpha_threshold.setValue(64)
    dialog.gif_dither.setChecked(False)

    assert dialog.path_edit.text().endswith("sample.gif")
    spec = dialog.specification()
    assert spec.path == tmp_path / "sample.gif"
    assert spec.options == GifExportOptions(32, 64, False)
    assert settings.last_export_format() is ExportFormat.GIF
    assert settings.last_export_directory() == tmp_path


def test_export_dialog_restores_per_format_options(qtbot, tmp_path: Path) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    first = ImageExportDialog(
        settings,
        "sample",
        tmp_path,
        _capabilities(),
    )
    qtbot.addWidget(first)
    first.format_combo.setCurrentIndex(first.format_combo.findData(ExportFormat.GIF.value))
    first.gif_colors.setValue(24)
    first.gif_alpha_threshold.setValue(200)
    first.gif_dither.setChecked(False)
    first.specification()

    restored = ImageExportDialog(
        settings,
        "again",
        tmp_path,
        _capabilities(),
    )
    qtbot.addWidget(restored)

    assert restored.export_format is ExportFormat.GIF
    assert restored.gif_colors.value() == 24
    assert restored.gif_alpha_threshold.value() == 200
    assert not restored.gif_dither.isChecked()


def test_export_dialog_falls_back_when_saved_encoder_is_unavailable(
    qtbot,
    tmp_path: Path,
) -> None:
    settings = make_settings(str(tmp_path / "settings.ini"))
    settings.set_export_state(ExportFormat.WEBP, tmp_path, "{}")
    dialog = ImageExportDialog(
        settings,
        "sample",
        tmp_path,
        _capabilities(ExportFormat.WEBP),
    )
    qtbot.addWidget(dialog)

    assert dialog.export_format is ExportFormat.PNG


def test_export_dialog_requires_confirmation_before_overwrite(
    monkeypatch,
    qtbot,
    tmp_path: Path,
) -> None:
    output = tmp_path / "sample.png"
    output.write_bytes(b"original")
    dialog = ImageExportDialog(
        make_settings(str(tmp_path / "settings.ini")),
        "sample",
        tmp_path,
        _capabilities(),
    )
    qtbot.addWidget(dialog)
    dialog.path_edit.setText(str(output))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Cancel,
    )

    dialog.accept()

    assert dialog.result() == QDialog.DialogCode.Rejected

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
