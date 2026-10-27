"""Local diagnostics and About dialog."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from easypicture.persistence.schema import PROJECT_SCHEMA_VERSION
from easypicture.render import CanvasRenderDiagnostics, RenderBackendKind
from easypicture.runtime import RuntimePaths, package_versions


@dataclass(frozen=True, slots=True)
class AboutDiagnostics:
    text: str


def build_diagnostics_report(
    paths: RuntimePaths,
    render: CanvasRenderDiagnostics | None,
) -> AboutDiagnostics:
    versions = package_versions()
    rows = [
        f"EasyPicture: {versions['EasyPicture']}",
        f"Python: {versions['Python']}",
        f"Qt / PySide6: {versions['Qt']} / {versions['PySide6']}",
        (
            "NumPy / Pillow / OpenCV: "
            f"{versions['NumPy']} / {versions['Pillow']} / {versions['OpenCV']}"
        ),
        f"项目格式: .limg schema v{PROJECT_SCHEMA_VERSION}",
    ]
    if render is None:
        rows.append("渲染后端: 尚未初始化")
    elif (
        render.requested_backend is not RenderBackendKind.SOFTWARE
        and render.active_backend is RenderBackendKind.SOFTWARE
        and not render.version
        and not render.fallback_reason
    ):
        rows.extend(
            (
                "渲染后端: OpenGL（等待画布初始化）",
                "GPU: 等待画布初始化",
                "OpenGL: 等待画布初始化（请求 3.3 Core）",
            )
        )
    else:
        opengl_status = render.version
        if not opengl_status:
            opengl_status = (
                "不可用（已回退到软件后端）" if render.fallback_reason else "未启用（使用软件后端）"
            )
        rows.extend(
            (
                f"渲染后端: {render.active_backend.value}",
                f"GPU: {render.vendor} {render.renderer}".strip(),
                f"OpenGL: {opengl_status}",
            )
        )
        if render.fallback_reason:
            rows.append(f"回退原因: {render.fallback_reason}")
    rows.extend(
        (
            f"应用数据: {paths.app_data}",
            f"缓存目录: {paths.cache}",
            f"日志目录: {paths.logs}",
        )
    )
    return AboutDiagnostics("\n".join(rows))


class AboutDialog(QDialog):
    def __init__(
        self,
        paths: RuntimePaths,
        render: CanvasRenderDiagnostics | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("关于 EasyPicture")
        self.setModal(True)
        self.setMinimumWidth(520)
        diagnostics = build_diagnostics_report(paths, render)
        self.diagnostics_text = diagnostics.text

        title = QLabel(
            f"<h2>EasyPicture {QCoreApplication.applicationVersion()}</h2>"
            "<p>本地离线轻量位图编辑器</p>",
            self,
        )
        detail = QLabel(self.diagnostics_text, self)
        detail.setTextInteractionFlags(detail.textInteractionFlags())
        detail.setWordWrap(True)

        copy_button = QPushButton("复制诊断信息", self)
        copy_button.clicked.connect(self._copy_diagnostics)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)

        form = QFormLayout()
        form.addRow(detail)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addWidget(copy_button)
        layout.addWidget(buttons)

    def _copy_diagnostics(self) -> None:
        clipboard = QGuiApplication.clipboard()
        clipboard.setText(self.diagnostics_text)


__all__ = [
    "AboutDiagnostics",
    "AboutDialog",
    "build_diagnostics_report",
]
