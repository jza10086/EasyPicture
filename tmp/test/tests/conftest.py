"""Shared test configuration."""

from __future__ import annotations

import os

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def virtual_pointer(monkeypatch):
    from PySide6.QtGui import QCursor
    from PySide6.QtWidgets import QWidget

    from easypicture.ui.pointer_backend import (
        VirtualPointerBackend,
        configure_pointer_backend,
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("native pointer operation is forbidden in automated tests")

    backend = VirtualPointerBackend()
    configure_pointer_backend(backend)
    monkeypatch.setattr(QCursor, "setPos", forbidden)
    monkeypatch.setattr(QWidget, "grabMouse", forbidden)
    monkeypatch.setattr(QWidget, "releaseMouse", forbidden)
    yield backend
    configure_pointer_backend(None)
