"""Runtime paths, rotating logs, and diagnostic information."""

from __future__ import annotations

import logging
import platform
import sys
import threading
from dataclasses import dataclass
from importlib import metadata
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QStandardPaths, QtMsgType, qInstallMessageHandler

from easypicture import __version__

LOGGER_NAME = "easypicture"
LOG_FILE_NAME = "easypicture.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    app_data: Path
    cache: Path
    logs: Path
    temp: Path

    @classmethod
    def from_qt(cls) -> RuntimePaths:
        app_data = _writable_path(QStandardPaths.StandardLocation.AppLocalDataLocation)
        cache = _writable_path(QStandardPaths.StandardLocation.CacheLocation)
        return cls(app_data, cache, app_data / "logs", cache / "temp")

    @classmethod
    def under(cls, root: Path) -> RuntimePaths:
        base = Path(root)
        app_data = base / "data"
        cache = base / "cache"
        return cls(app_data, cache, app_data / "logs", cache / "temp")


def configure_logging(paths: RuntimePaths, *, level: int = logging.INFO) -> Path:
    paths.logs.mkdir(parents=True, exist_ok=True)
    log_path = paths.logs / LOG_FILE_NAME
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    for handler in tuple(logger.handlers):
        if isinstance(handler, RotatingFileHandler):
            handler.close()
        logger.removeHandler(handler)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    return log_path


def install_exception_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    previous_sys_hook = sys.excepthook
    previous_thread_hook = threading.excepthook

    def sys_hook(
        exception_type: type[BaseException],
        exception: BaseException,
        traceback: TracebackType | None,
    ) -> None:
        logger.critical(
            "unhandled main-thread exception",
            exc_info=(exception_type, exception, traceback),
        )
        previous_sys_hook(exception_type, exception, traceback)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        logger.critical(
            "unhandled worker-thread exception in %s",
            args.thread.name if args.thread is not None else "unknown",
            exc_info=args.exc_value,
        )
        previous_thread_hook(args)

    def qt_message_handler(
        message_type: QtMsgType,
        _context: object,
        message: str,
    ) -> None:
        levels = {
            QtMsgType.QtDebugMsg: logging.DEBUG,
            QtMsgType.QtInfoMsg: logging.INFO,
            QtMsgType.QtWarningMsg: logging.WARNING,
            QtMsgType.QtCriticalMsg: logging.ERROR,
            QtMsgType.QtFatalMsg: logging.CRITICAL,
        }
        logger.log(levels.get(message_type, logging.INFO), "Qt: %s", message)

    sys.excepthook = sys_hook
    threading.excepthook = thread_hook
    qInstallMessageHandler(qt_message_handler)


def log_runtime_environment() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.info(
        "starting EasyPicture %s on Python %s (%s)",
        __version__,
        platform.python_version(),
        platform.platform(),
    )
    versions = package_versions()
    logger.info(
        "dependencies: Qt=%s PySide6=%s NumPy=%s Pillow=%s OpenCV=%s",
        versions["Qt"],
        versions["PySide6"],
        versions["NumPy"],
        versions["Pillow"],
        versions["OpenCV"],
    )


def package_versions() -> dict[str, str]:
    import cv2
    import numpy as np
    import PIL
    import PySide6
    from PySide6.QtCore import qVersion

    return {
        "EasyPicture": __version__,
        "Python": platform.python_version(),
        "Qt": qVersion(),
        "PySide6": _module_version(PySide6, "PySide6"),
        "NumPy": _module_version(np, "numpy"),
        "Pillow": _module_version(PIL, "Pillow"),
        "OpenCV": _module_version(cv2, "opencv-python-headless"),
    }


def _module_version(module: object, distribution_name: str) -> str:
    """Read runtime module data first because standalone builds omit dist-info."""
    value = getattr(module, "__version__", None)
    if value is not None and str(value).strip():
        return str(value)
    return _distribution_version(distribution_name)


def _distribution_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "不可用"


def _writable_path(location: QStandardPaths.StandardLocation) -> Path:
    raw = QStandardPaths.writableLocation(location)
    if not raw:
        raise RuntimeError(f"Qt did not provide a writable runtime path: {location!r}")
    return Path(raw)


__all__ = [
    "LOGGER_NAME",
    "LOG_BACKUP_COUNT",
    "LOG_FILE_NAME",
    "LOG_MAX_BYTES",
    "RuntimePaths",
    "configure_logging",
    "install_exception_logging",
    "log_runtime_environment",
    "package_versions",
]
