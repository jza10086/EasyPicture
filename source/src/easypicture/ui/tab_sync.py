"""Keyed tab updates that preserve existing tabs and their view state."""

from collections.abc import Sequence

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QTabBar


def synchronize_tabs(
    tabs: QTabBar,
    entries: Sequence[tuple[object, str]],
    active_key: object,
) -> None:
    blocker = QSignalBlocker(tabs)
    try:
        keys = [key for key, _label in entries]
        for index in range(tabs.count() - 1, -1, -1):
            if tabs.tabData(index) not in keys:
                tabs.removeTab(index)
        for target, (key, label) in enumerate(entries):
            existing = next(
                (index for index in range(tabs.count()) if tabs.tabData(index) == key),
                None,
            )
            if existing is None:
                tabs.insertTab(target, label)
                tabs.setTabData(target, key)
            elif existing != target:
                tabs.moveTab(existing, target)
            if tabs.tabText(target) != label:
                tabs.setTabText(target, label)
            if key == active_key:
                tabs.setCurrentIndex(target)
    finally:
        blocker.unblock()
