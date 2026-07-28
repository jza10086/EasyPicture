"""Minimal P0 Qt application shell."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from easypicture import __version__
from easypicture.controller import ApplicationController
from easypicture.domain import Document
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    DocumentSnapshot,
    export_png,
    import_image,
    load_project,
    save_project,
)
from easypicture.render import RenderBackendKind
from easypicture.render_smoke import run_render_smoke_test
from easypicture.ui.dialogs import DialogService, QtDialogService
from easypicture.ui.main_window import MainWindow
from easypicture.ui.settings import AppSettings
from easypicture.ui.theme import apply_dark_theme


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="easypicture")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Create the Qt shell, process the event loop once, and exit.",
    )
    parser.add_argument(
        "--io-smoke-test",
        type=Path,
        metavar="DIR",
        help="Run a tiny project/import/export round trip under DIR and exit.",
    )
    parser.add_argument(
        "--render-backend",
        choices=[backend.value for backend in RenderBackendKind],
        help="Override the canvas renderer for this process.",
    )
    parser.add_argument(
        "--render-smoke-test",
        type=Path,
        metavar="DIR",
        help="Render a GPU/software reference scene under DIR and exit.",
    )
    return parser


def create_window(
    *,
    settings: AppSettings | None = None,
    dialogs: DialogService | None = None,
    recovery_enabled: bool = True,
    render_backend: RenderBackendKind | None = None,
) -> MainWindow:
    """Create the P1 main window with injectable settings and dialogs."""
    resolved_settings = settings if settings is not None else AppSettings()
    resolved_dialogs = dialogs if dialogs is not None else QtDialogService()
    controller = ApplicationController(
        resolved_settings,
        resolved_dialogs,
        recovery_enabled=recovery_enabled,
    )
    return MainWindow(
        controller,
        resolved_settings,
        render_backend=render_backend,
    )


def run(argv: Sequence[str] | None = None) -> int:
    """Run the Qt application and return its process exit code."""
    options = build_parser().parse_args(argv)
    if options.io_smoke_test is not None:
        return _run_io_smoke_test(options.io_smoke_test)
    requested_backend = (
        RenderBackendKind(options.render_backend)
        if options.render_backend is not None
        else None
    )
    _configure_surface_format(requested_backend)
    app = QApplication(["easypicture"])
    app.setOrganizationName("EasyPicture")
    app.setOrganizationDomain("local.easypicture")
    app.setApplicationName("EasyPicture")
    app.setApplicationVersion(__version__)
    apply_dark_theme(app)

    if options.render_smoke_test is not None:
        smoke_backend = requested_backend or RenderBackendKind.OPENGL
        return run_render_smoke_test(
            app,
            options.render_smoke_test,
            smoke_backend,
        )

    window = create_window(
        recovery_enabled=not options.smoke_test,
        render_backend=requested_backend,
    )
    window.show()

    if options.smoke_test:
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(0, app.quit)
    else:
        QTimer.singleShot(0, lambda: window.controller.offer_recovery(window))

    return int(app.exec())


def _configure_surface_format(
    requested_backend: RenderBackendKind | None,
) -> None:
    if requested_backend is RenderBackendKind.SOFTWARE:
        return
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
    surface_format = QSurfaceFormat()
    surface_format.setVersion(3, 3)
    surface_format.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    surface_format.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
    surface_format.setAlphaBufferSize(8)
    surface_format.setDepthBufferSize(0)
    surface_format.setStencilBufferSize(0)
    surface_format.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
    QSurfaceFormat.setDefaultFormat(surface_format)


def _run_io_smoke_test(directory: Path) -> int:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        document = Document.create(2, 2)
        tile = np.zeros((256, 256, 4), dtype=np.uint8)
        tile[0, 0] = (255, 0, 0, 128)
        document.active_layer.pixels.replace_tile(0, 0, tile)
        snapshot = DocumentSnapshot.capture(document)
        token = CancellationToken()

        def progress(_done: int, _total: int, _message: str) -> None:
            return

        project_path = directory / "io-smoke.limg"
        png_path = directory / "io-smoke.png"
        save_project(snapshot, project_path, token, progress)
        loaded = load_project(project_path, token, progress)
        export_png(loaded.snapshot, png_path, token, progress)
        imported = import_image(png_path, token, progress)
        pixel = imported.document.active_layer.pixels.read_tile(0, 0)[0, 0]
        return 0 if tuple(pixel) == (255, 0, 0, 128) else 1
    except Exception:
        return 1


__all__ = ["build_parser", "create_window", "run"]
