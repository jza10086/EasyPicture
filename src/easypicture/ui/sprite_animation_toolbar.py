"""Compact controls for a read-only sprite animation surface."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from easypicture.domain import SpriteAnimation, SpriteFrameDefinition, SpritePlaybackMode
from easypicture.ui.blender_combo_box import BlenderComboBox
from easypicture.ui.drag_value_spinbox import DragValueSpinBox
from easypicture.ui.settings import AppSettings, OnionSkinSettings


class OnionSkinSettingsDialog(QDialog):
    def __init__(
        self,
        current: OnionSkinSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("洋葱皮设置")
        self.previous_count = DragValueSpinBox(0, 3, current.previous_count, parent=self)
        self.next_count = DragValueSpinBox(0, 3, current.next_count, parent=self)
        self.opacity = DragValueSpinBox(
            0, 100, current.opacity_percent, suffix="%", parent=self
        )
        self.falloff = DragValueSpinBox(
            0, 100, current.falloff_percent, suffix="%", parent=self
        )
        self._previous_color = QColor(current.previous_color)
        self._next_color = QColor(current.next_color)
        self.previous_color_button = QPushButton("前帧颜色", self)
        self.next_color_button = QPushButton("后帧颜色", self)
        self.previous_color_button.clicked.connect(
            lambda: self._choose_color(previous=True)
        )
        self.next_color_button.clicked.connect(
            lambda: self._choose_color(previous=False)
        )
        self._update_color_buttons()
        form = QFormLayout()
        form.addRow("前帧数量", self.previous_count)
        form.addRow("后帧数量", self.next_count)
        form.addRow("基础透明度", self.opacity)
        form.addRow("距离衰减", self.falloff)
        form.addRow("前帧着色", self.previous_color_button)
        form.addRow("后帧着色", self.next_color_button)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QFormLayout(self)
        layout.addRow(form)
        layout.addRow(buttons)

    @property
    def value(self) -> OnionSkinSettings:
        return OnionSkinSettings(
            previous_count=self.previous_count.value(),
            next_count=self.next_count.value(),
            opacity_percent=self.opacity.value(),
            falloff_percent=self.falloff.value(),
            previous_color=QColor(self._previous_color),
            next_color=QColor(self._next_color),
            enabled=True,
        )

    def _choose_color(self, *, previous: bool) -> None:
        current = self._previous_color if previous else self._next_color
        selected = QColorDialog.getColor(current, self, "选择洋葱皮颜色")
        if not selected.isValid():
            return
        selected.setAlpha(255)
        if previous:
            self._previous_color = selected
        else:
            self._next_color = selected
        self._update_color_buttons()

    def _update_color_buttons(self) -> None:
        self.previous_color_button.setStyleSheet(
            f"background-color: {self._previous_color.name()};"
        )
        self.next_color_button.setStyleSheet(
            f"background-color: {self._next_color.name()};"
        )


class SpriteAnimationToolbar(QWidget):
    first_requested = Signal()
    previous_requested = Signal()
    play_toggled = Signal(bool)
    next_requested = Signal()
    last_requested = Signal()
    fps_requested = Signal(int)
    mode_requested = Signal(object)
    onion_settings_changed = Signal(object)

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._syncing = False
        self.first_button = QPushButton("|◀", self)
        self.previous_button = QPushButton("◀", self)
        self.play_button = QPushButton("播放", self)
        self.play_button.setCheckable(True)
        self.next_button = QPushButton("▶", self)
        self.last_button = QPushButton("▶|", self)
        self.position_label = QLabel("0 / 0", self)
        self.fps = DragValueSpinBox(
            1,
            60,
            12,
            manual_minimum=1,
            manual_maximum=240,
            suffix=" FPS",
            parent=self,
        )
        self.mode = BlenderComboBox(self)
        for mode, label in (
            (SpritePlaybackMode.ONCE, "单次"),
            (SpritePlaybackMode.LOOP, "循环"),
            (SpritePlaybackMode.PING_PONG, "往返"),
        ):
            self.mode.addItem(label, mode)
        onion = settings.onion_skin_settings()
        self.onion = QCheckBox("洋葱皮", self)
        self.onion.setChecked(onion.enabled)
        self.onion_settings_button = QPushButton("参数…", self)
        self.duration_label = QLabel("总时长 0.00 s", self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)
        for widget in (
            self.first_button,
            self.previous_button,
            self.play_button,
            self.next_button,
            self.last_button,
            self.position_label,
            self.fps,
            self.mode,
            self.onion,
            self.onion_settings_button,
            self.duration_label,
        ):
            layout.addWidget(widget)
        layout.addStretch(1)
        self.first_button.clicked.connect(self.first_requested.emit)
        self.previous_button.clicked.connect(self.previous_requested.emit)
        self.play_button.toggled.connect(self._play_changed)
        self.next_button.clicked.connect(self.next_requested.emit)
        self.last_button.clicked.connect(self.last_requested.emit)
        self.fps.editingFinished.connect(self._fps_finished)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.onion.toggled.connect(self._onion_toggled)
        self.onion_settings_button.clicked.connect(self._edit_onion_settings)
        self.hide()

    def set_animation(
        self,
        animation: SpriteAnimation,
        frames: dict[object, SpriteFrameDefinition],
        index: int,
        *,
        playing: bool,
    ) -> None:
        self._syncing = True
        try:
            self.fps.syncValue(animation.fps)
            mode_index = self.mode.findData(animation.playback_mode)
            if mode_index >= 0:
                self.mode.setCurrentIndex(mode_index)
            count = len(animation.frame_ids)
            self.position_label.setText(f"{index + 1 if count else 0} / {count}")
            ticks = sum(frames[frame_id].duration_ticks for frame_id in animation.frame_ids)
            self.duration_label.setText(f"总时长 {ticks / animation.fps:.2f} s")
            self.play_button.setChecked(playing)
            self.play_button.setText("暂停" if playing else "播放")
        finally:
            self._syncing = False

    def _play_changed(self, checked: bool) -> None:
        self.play_button.setText("暂停" if checked else "播放")
        if not self._syncing:
            self.play_toggled.emit(checked)

    def _fps_finished(self) -> None:
        if not self._syncing:
            self.fps_requested.emit(self.fps.value())

    def _mode_changed(self, index: int) -> None:
        mode = self.mode.itemData(index)
        if self._syncing:
            return
        try:
            resolved = SpritePlaybackMode(str(mode))
        except ValueError:
            return
        self.mode_requested.emit(resolved)

    def _onion_toggled(self, enabled: bool) -> None:
        if self._syncing:
            return
        current = self._settings.onion_skin_settings()
        updated = OnionSkinSettings(
            current.previous_count,
            current.next_count,
            current.opacity_percent,
            current.falloff_percent,
            QColor(current.previous_color),
            QColor(current.next_color),
            enabled,
        )
        self._settings.set_onion_skin_settings(updated)
        self.onion_settings_changed.emit(updated)

    def _edit_onion_settings(self) -> None:
        dialog = OnionSkinSettingsDialog(
            self._settings.onion_skin_settings(), self
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        value = dialog.value
        value = OnionSkinSettings(
            value.previous_count,
            value.next_count,
            value.opacity_percent,
            value.falloff_percent,
            value.previous_color,
            value.next_color,
            self.onion.isChecked(),
        )
        self._settings.set_onion_skin_settings(value)
        self.onion_settings_changed.emit(value)


__all__ = ["OnionSkinSettingsDialog", "SpriteAnimationToolbar"]
