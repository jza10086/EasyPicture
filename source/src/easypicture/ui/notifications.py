"""Deduplicated, non-modal user notices."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtCore import QObject, Signal


class NoticeSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class UserNotice:
    key: str
    severity: NoticeSeverity
    message: str
    details: str = ""


class NotificationService(QObject):
    """Publish status-bar notices while suppressing rapid duplicates."""

    notice = Signal(object)

    def __init__(self, parent: QObject | None = None, *, dedupe_seconds: float = 5.0) -> None:
        super().__init__(parent)
        self._dedupe_seconds = dedupe_seconds
        self._last_emitted: dict[str, float] = {}

    def publish(
        self,
        key: str,
        severity: NoticeSeverity,
        message: str,
        *,
        details: str = "",
    ) -> bool:
        now = time.monotonic()
        previous = self._last_emitted.get(key)
        if previous is not None and now - previous < self._dedupe_seconds:
            return False
        self._last_emitted[key] = now
        notice = UserNotice(key, severity, message, details)
        logging.getLogger("easypicture.notifications").log(
            {
                NoticeSeverity.INFO: logging.INFO,
                NoticeSeverity.WARNING: logging.WARNING,
                NoticeSeverity.ERROR: logging.ERROR,
            }[severity],
            "%s%s",
            message,
            f" ({details})" if details else "",
        )
        self.notice.emit(notice)
        return True

    def info(self, key: str, message: str) -> bool:
        return self.publish(key, NoticeSeverity.INFO, message)

    def warning(self, key: str, message: str, *, details: str = "") -> bool:
        return self.publish(
            key,
            NoticeSeverity.WARNING,
            message,
            details=details,
        )


__all__ = [
    "NoticeSeverity",
    "NotificationService",
    "UserNotice",
]
