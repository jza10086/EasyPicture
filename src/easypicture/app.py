"""Minimal P0 Qt application shell."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMainWindow

from easypicture import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="easypicture")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Create the Qt shell, process the event loop once, and exit.",
    )
    return parser


def create_window() -> QMainWindow:
    """Create the intentionally minimal P0 main window."""
    window = QMainWindow()
    window.setWindowTitle(f"EasyPicture {__version__}")
    window.resize(960, 640)
    return window


def run(argv: Sequence[str] | None = None) -> int:
    """Run the Qt application and return its process exit code."""
    options = build_parser().parse_args(argv)
    app = QApplication(["easypicture"])
    app.setApplicationName("EasyPicture")
    app.setApplicationVersion(__version__)

    window = create_window()
    window.show()

    if options.smoke_test:
        QTimer.singleShot(0, window.close)
        QTimer.singleShot(0, app.quit)

    return int(app.exec())


__all__ = ["build_parser", "create_window", "run"]
