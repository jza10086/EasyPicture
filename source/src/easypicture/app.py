"""Minimal P0 Qt application shell."""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import QApplication

from easypicture import __version__
from easypicture.controller import ApplicationController
from easypicture.domain import Document, SpriteFrameDefinition
from easypicture.jobs import CancellationToken
from easypicture.persistence import (
    PROJECT_SCHEMA_VERSION,
    BmpExportOptions,
    DocumentSnapshot,
    GifExportOptions,
    ImageExportOptions,
    ImageExportRequest,
    JpegExportOptions,
    PngExportOptions,
    SpriteFrameExportRequest,
    SvgExportOptions,
    SvgVectorExportMode,
    TgaExportOptions,
    TiffExportOptions,
    WebpExportOptions,
    export_image,
    export_png,
    export_sprite_frame_png,
    import_image,
    import_svg,
    load_project,
    save_project,
)
from easypicture.pixels import Rect
from easypicture.render import RenderBackendKind
from easypicture.render_smoke import run_render_smoke_test
from easypicture.runtime import (
    RuntimePaths,
    configure_logging,
    install_exception_logging,
    log_runtime_environment,
)
from easypicture.temp_files import TemporaryFileManager, configure_temp_root
from easypicture.ui.dialogs import DialogService, QtDialogService
from easypicture.ui.main_window import MainWindow
from easypicture.ui.settings import AppSettings
from easypicture.ui.theme import apply_dark_theme
from easypicture.workspace import WorkspaceController


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
    parser.add_argument(
        "--export-smoke-test",
        type=Path,
        metavar="DIR",
        help="Export and reopen all supported still-image formats under DIR.",
    )
    parser.add_argument(
        "--svg-smoke-test",
        type=Path,
        metavar="DIR",
        help="Import, persist, export and rasterize a static SVG under DIR.",
    )
    return parser


def create_window(
    *,
    settings: AppSettings | None = None,
    dialogs: DialogService | None = None,
    recovery_enabled: bool = True,
    render_backend: RenderBackendKind | None = None,
    runtime_paths: RuntimePaths | None = None,
) -> MainWindow:
    """Create the P1 main window with injectable settings and dialogs."""
    resolved_settings = settings if settings is not None else AppSettings()
    resolved_dialogs = (
        dialogs if dialogs is not None else QtDialogService(resolved_settings, runtime_paths)
    )
    initial_controller = ApplicationController(
        resolved_settings,
        resolved_dialogs,
        recovery_enabled=recovery_enabled,
    )
    controller = WorkspaceController(
        resolved_settings,
        resolved_dialogs,
        recovery_enabled=recovery_enabled,
        initial_controller=initial_controller,
    )
    return MainWindow(
        controller,
        resolved_settings,
        render_backend=render_backend,
        runtime_paths=runtime_paths,
    )


def run(argv: Sequence[str] | None = None) -> int:
    """Run the Qt application and return its process exit code."""
    options = build_parser().parse_args(argv)
    if options.io_smoke_test is not None:
        return _run_io_smoke_test(options.io_smoke_test)
    if options.export_smoke_test is not None:
        return _run_export_smoke_test(options.export_smoke_test)
    if options.svg_smoke_test is not None:
        return _run_svg_smoke_test(options.svg_smoke_test)
    requested_backend = (
        RenderBackendKind(options.render_backend) if options.render_backend is not None else None
    )
    _configure_surface_format(requested_backend)
    app = QApplication(["easypicture"])
    app.setOrganizationName("EasyPicture")
    app.setOrganizationDomain("local.easypicture")
    app.setApplicationName("EasyPicture")
    app.setApplicationVersion(__version__)
    apply_dark_theme(app)
    runtime_paths = _runtime_paths_for_options(
        smoke_test=options.smoke_test,
        render_smoke_directory=options.render_smoke_test,
    )
    startup_warnings: list[str] = []
    try:
        configure_logging(runtime_paths)
    except OSError as error:
        fallback_root = Path(tempfile.mkdtemp(prefix="easypicture-runtime-"))
        runtime_paths = RuntimePaths.under(fallback_root)
        configure_logging(runtime_paths)
        startup_warnings.append(f"默认日志目录不可写，已使用临时目录：{error}")
    install_exception_logging()
    log_runtime_environment()
    temp_manager = configure_temp_root(runtime_paths.temp)
    cleanup_report = temp_manager.cleanup_stale()
    logger = logging.getLogger("easypicture")
    logger.info(
        "startup temporary cleanup: entries=%s bytes=%s failures=%s",
        cleanup_report.removed_entries,
        cleanup_report.removed_bytes,
        len(cleanup_report.failures),
    )
    app.aboutToQuit.connect(lambda: _shutdown_runtime(temp_manager))

    if options.render_smoke_test is not None:
        smoke_backend = requested_backend or RenderBackendKind.OPENGL
        exit_code = run_render_smoke_test(
            app,
            options.render_smoke_test,
            smoke_backend,
        )
        _shutdown_runtime(temp_manager)
        return exit_code

    window = create_window(
        recovery_enabled=not options.smoke_test,
        render_backend=requested_backend,
        runtime_paths=runtime_paths,
    )
    _present_main_window(window)
    if cleanup_report.failures:
        startup_warnings.append("部分过期临时文件无法清理；软件仍可正常使用")
    if startup_warnings:
        QTimer.singleShot(
            0,
            lambda: window.controller.notifications.warning(
                "runtime-startup-warning",
                startup_warnings[0],
                details="\n".join((*startup_warnings, *cleanup_report.failures)),
            ),
        )

    if options.smoke_test:
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(0, app.quit)
    else:
        QTimer.singleShot(0, lambda: _present_main_window(window))
        QTimer.singleShot(150, lambda: window.controller.offer_recovery(window))

    return int(app.exec())


def _present_main_window(window: MainWindow) -> None:
    window.show()
    window.raise_()
    window.activateWindow()
    geometry = window.frameGeometry()
    logging.getLogger("easypicture").info(
        "main window presented: visible=%s active=%s geometry=%s,%s %sx%s",
        window.isVisible(),
        window.isActiveWindow(),
        geometry.x(),
        geometry.y(),
        geometry.width(),
        geometry.height(),
    )


def _shutdown_runtime(temp_manager: TemporaryFileManager) -> None:
    report = temp_manager.cleanup_current_session()
    logging.getLogger("easypicture").info(
        "shutdown temporary cleanup: entries=%s bytes=%s failures=%s",
        report.removed_entries,
        report.removed_bytes,
        len(report.failures),
    )
    logging.getLogger("easypicture").info("EasyPicture shutdown")


def _runtime_paths_for_options(
    *,
    smoke_test: bool,
    render_smoke_directory: Path | None,
) -> RuntimePaths:
    if render_smoke_directory is not None:
        return RuntimePaths.under(render_smoke_directory / "runtime")
    if smoke_test:
        root = Path(tempfile.mkdtemp(prefix="easypicture-smoke-"))
        return RuntimePaths.under(root)
    return RuntimePaths.from_qt()


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


def _run_export_smoke_test(directory: Path) -> int:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        document = Document.create(4, 4)
        tile = np.zeros((256, 256, 4), dtype=np.uint8)
        tile[:2, :2] = (255, 0, 0, 255)
        tile[:2, 2:4] = (0, 255, 0, 128)
        tile[2:4, :2] = (12, 34, 56, 0)
        tile[2:4, 2:4] = (0, 0, 255, 255)
        document.active_layer.pixels.replace_tile(0, 0, tile)
        sprite_frame = SpriteFrameDefinition.create("smoke_frame", Rect(0, 0, 4, 4))
        document.sprite_workspace.frames.append(sprite_frame)
        snapshot = DocumentSnapshot.capture(document)
        token = CancellationToken()

        def progress(_done: int, _total: int, _message: str) -> None:
            return

        cases: dict[str, ImageExportOptions] = {
            "png": PngExportOptions(),
            "jpg": JpegExportOptions(),
            "webp": WebpExportOptions(),
            "bmp": BmpExportOptions(),
            "tiff": TiffExportOptions(),
            "tga": TgaExportOptions(),
            "gif": GifExportOptions(),
        }
        diagnostics: dict[str, object] = {"formats": {}}
        format_results = diagnostics["formats"]
        if not isinstance(format_results, dict):
            return 1
        for extension, export_options in cases.items():
            destination = directory / f"export-smoke.{extension}"
            receipt = export_image(
                ImageExportRequest(snapshot, destination, export_options),
                token,
                progress,
            )
            with Image.open(receipt.path) as image:
                image.load()
                format_results[receipt.format.value] = {
                    "path": receipt.path.name,
                    "format": image.format,
                    "size": list(image.size),
                    "frames": int(getattr(image, "n_frames", 1)),
                    "bytes": receipt.file_size,
                }
        sprite_receipt = export_sprite_frame_png(
            SpriteFrameExportRequest(
                snapshot,
                sprite_frame.id,
                directory / "export-smoke-frame.png",
            ),
            token,
            progress,
        )
        diagnostics["sprite_frame"] = {
            "path": sprite_receipt.path.name,
            "size": [sprite_receipt.width, sprite_receipt.height],
            "bytes": sprite_receipt.file_size,
        }
        diagnostics_path = directory / "diagnostics.json"
        diagnostics_path.write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0
    except Exception:
        return 1


def _run_svg_smoke_test(directory: Path) -> int:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        app = QApplication.instance() or QApplication(["easypicture-svg-smoke"])
        _ = app
        source = directory / "svg-smoke-source.svg"
        source.write_text(
            """
            <svg xmlns="http://www.w3.org/2000/svg" width="128" height="96">
              <rect id="background" x="8" y="8" width="112" height="80"
                    rx="8" fill="#315a8a"/>
              <circle id="marker" cx="64" cy="48" r="22" fill="#ff8a00"/>
              <text id="caption" x="38" y="54" font-size="16">SVG</text>
            </svg>
            """,
            encoding="utf-8",
        )
        token = CancellationToken()

        def progress(_done: int, _total: int, _message: str) -> None:
            return

        imported = import_svg(source, token, progress)
        snapshot = DocumentSnapshot.capture(imported.document)
        project = directory / "svg-smoke.limg"
        save_project(snapshot, project, token, progress)
        reopened = load_project(project, token, progress)
        preserved = export_image(
            ImageExportRequest(
                reopened.snapshot,
                directory / "svg-smoke-preserved.svg",
                SvgExportOptions(),
            ),
            token,
            progress,
        )
        rasterized = export_image(
            ImageExportRequest(
                reopened.snapshot,
                directory / "svg-smoke-rasterized.svg",
                SvgExportOptions(SvgVectorExportMode.RASTERIZE_VECTOR),
            ),
            token,
            progress,
        )
        diagnostics = {
            "imported": imported.imported_count,
            "skipped": imported.skipped_count,
            "project": project.name,
            "preserved": preserved.path.name,
            "rasterized": rasterized.path.name,
            "schema": PROJECT_SCHEMA_VERSION,
        }
        (directory / "svg-diagnostics.json").write_text(
            json.dumps(diagnostics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return 0
    except Exception:
        logging.getLogger("easypicture.svg_smoke").exception("SVG smoke failed")
        return 1


__all__ = ["build_parser", "create_window", "run"]
