"""P2 single-document main window with layer management and undo."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QKeySequence
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from easypicture import __version__
from easypicture.controller import ApplicationController
from easypicture.domain import Document, DocumentEvent, Point
from easypicture.tools import (
    BrushTool,
    BucketTool,
    EyedropperTool,
    NullTool,
    Tool,
    ToolContext,
    ToolController,
)
from easypicture.ui.canvas_view import CanvasView
from easypicture.ui.color_panel import ColorPanel
from easypicture.ui.layer_panel import LayerPanel
from easypicture.ui.overlays import OverlayManager
from easypicture.ui.settings import AppSettings
from easypicture.ui.tool_options import ToolOptionsPanel

RIGHT_SIDEBAR_STYLE = """
QMainWindow#MainWindow {
    background-color: #202020;
}
QMainWindow#MainWindow::separator {
    background-color: #151515;
    width: 4px;
    height: 4px;
}
QMainWindow#MainWindow QTabBar {
    background-color: #202020;
}
QMainWindow#MainWindow QTabBar::tab {
    background-color: #292929;
    border: 1px solid #151515;
    margin: 2px 1px 0 1px;
    padding: 4px 10px;
}
QMainWindow#MainWindow QTabBar::tab:selected {
    background-color: #424242;
    border-bottom-color: #4772b3;
}
"""

RIGHT_DOCK_STYLE = """
QDockWidget[panelRole="right"] {
    background-color: #292929;
    border: 1px solid #151515;
}
QDockWidget[panelRole="right"]::title {
    color: #dddddd;
    background-color: #303030;
    border-bottom: 1px solid #151515;
    padding: 5px 8px;
}
QWidget[panelRole="rightSidebar"] {
    background-color: #292929;
}
QFrame[panelRole="dockCard"] {
    background-color: #2b2b2b;
    border: 1px solid #171717;
    border-radius: 3px;
}
"""


class EmptyStateWidget(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("EmptyState")
        self.new_button = QPushButton("新建文档", self)
        self.open_button = QPushButton("打开图片或项目", self)
        title = QLabel("EasyPicture", self)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        hint = QLabel("创建一个透明画布以开始工作", self)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self.new_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.open_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(
        self,
        controller: ApplicationController,
        settings: AppSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.controller.set_dialog_parent(self)
        self.setObjectName("MainWindow")
        self.setStyleSheet(RIGHT_SIDEBAR_STYLE)
        self.resize(1100, 720)
        self._editing_busy = False

        self.canvas_view = CanvasView(self)
        self.empty_state = EmptyStateWidget(self)
        self.central_stack = QStackedWidget(self)
        self.central_stack.addWidget(self.empty_state)
        self.central_stack.addWidget(self.canvas_view)
        self.setCentralWidget(self.central_stack)

        self.overlay_manager = OverlayManager(self.canvas_view.scene())
        self.tool_context = ToolContext(
            canvas=self.canvas_view,
            overlays=self.overlay_manager,
            document_provider=lambda: self.controller.document,
            commands=self.controller.commands,
            foreground_provider=self._foreground_rgba,
            set_foreground=self._set_foreground_rgba,
            set_background=self._set_background_rgba,
            jobs=self.controller.jobs,
            set_busy=self._set_editing_busy,
            status=self.controller.status_message.emit,
        )
        self.tool_controller = ToolController(self.tool_context, NullTool())
        self.canvas_view.set_tool_controller(self.tool_controller)

        self._create_actions()
        self._create_menus()
        self._create_toolbars()
        self._create_docks()
        self._create_status_bar()
        self._connect_signals()
        self._on_document_changed(self.controller.document)
        self.settings.restore_window(self)

    def _create_actions(self) -> None:
        self.new_action = QAction("新建(&N)", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.open_action = QAction("打开(&O)", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.close_action = QAction("关闭文档(&C)", self)
        self.close_action.setShortcut(QKeySequence.StandardKey.Close)
        self.exit_action = QAction("退出(&X)", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)

        self.save_action = QAction("保存(&S)", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_as_action = QAction("另存为", self)
        self.export_action = QAction("导出 PNG", self)
        for action in (self.save_action, self.save_as_action, self.export_action):
            action.setEnabled(False)

        self.undo_action = QAction("撤销", self)
        self.redo_action = QAction("重做", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.undo_action.setEnabled(False)
        self.redo_action.setEnabled(False)

        self.zoom_in_action = QAction("放大", self)
        self.zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.zoom_out_action = QAction("缩小", self)
        self.zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.fit_action = QAction("适合窗口", self)
        self.fit_action.setShortcut(QKeySequence("Ctrl+0"))
        self.actual_size_action = QAction("100%", self)
        self.actual_size_action.setShortcut(QKeySequence("Ctrl+1"))

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.close_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addAction(self.export_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        edit_menu = self.menuBar().addMenu("编辑(&E)")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.actual_size_action)

    def _create_toolbars(self) -> None:
        self.tool_options = QToolBar("工具选项", self)
        self.tool_options.setObjectName("ToolOptionsToolbar")
        self.tool_options_panel = ToolOptionsPanel(self.settings, self.tool_options)
        self.tool_options.addWidget(self.tool_options_panel)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.tool_options)

        self.tools_toolbar = QToolBar("工具", self)
        self.tools_toolbar.setObjectName("ToolsToolbar")
        self.tools_toolbar.setOrientation(Qt.Orientation.Vertical)
        self.tool_action_group = QActionGroup(self)
        self.tool_action_group.setExclusive(True)
        self._tools: dict[str, Tool] = {
            "brush": BrushTool(self.tool_options_panel.brush.settings),
            "eraser": BrushTool(self.tool_options_panel.brush.settings, eraser=True),
            "eyedropper": EyedropperTool(self.tool_options_panel.eyedropper.sample_merged),
            "bucket": BucketTool(self.tool_options_panel.bucket.settings),
        }
        self.tool_actions: dict[str, QAction] = {}
        for name, label, shortcut in (
            ("brush", "画笔", "B"),
            ("eraser", "橡皮", "E"),
            ("eyedropper", "吸管", "I"),
            ("bucket", "油漆桶", "G"),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setShortcut(QKeySequence(shortcut))
            action.setData(name)
            action.triggered.connect(
                lambda _checked=False, tool_name=name: self._activate_tool(tool_name)
            )
            self.tool_action_group.addAction(action)
            self.tools_toolbar.addAction(action)
            self.tool_actions[name] = action
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, self.tools_toolbar)
        self.tool_options_panel.brush_settings_changed.connect(self._save_brush_settings)
        self.tool_options_panel.bucket_settings_changed.connect(self._save_bucket_settings)
        self.tool_options_panel.eyedropper_settings_changed.connect(
            self._save_eyedropper_settings
        )
        self.tool_actions["brush"].setChecked(True)
        self._activate_tool("brush")

    def _create_docks(self) -> None:
        self.layer_panel = LayerPanel(self.controller.commands, self)
        self.layers_dock = self._make_dock("图层", "LayersDock", self.layer_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.layers_dock)

        self.color_panel = ColorPanel(self.settings, self.controller.dialogs, self)
        self.color_dock = self._make_dock("颜色", "ColorDock", self.color_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.color_dock)

        properties = QLabel("属性面板将在后续阶段提供", self)
        properties.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.properties_dock = self._make_dock("属性", "PropertiesDock", properties)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.properties_dock)
        self.tabifyDockWidget(self.color_dock, self.properties_dock)

    def _create_status_bar(self) -> None:
        self.coordinate_label = QLabel("x: —  y: —", self)
        self.document_size_label = QLabel("无文档", self)
        self.zoom_label = QLabel("100%", self)
        self.coordinate_label.setMinimumWidth(120)
        self.document_size_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.statusBar().addWidget(self.coordinate_label)
        self.statusBar().addWidget(self.document_size_label, 1)
        self.statusBar().addPermanentWidget(self.zoom_label)

    def _connect_signals(self) -> None:
        self.empty_state.new_button.clicked.connect(self._request_new_document)
        self.empty_state.open_button.clicked.connect(self._request_open_document)
        self.new_action.triggered.connect(self._request_new_document)
        self.open_action.triggered.connect(self._request_open_document)
        self.close_action.triggered.connect(self._request_close_document)
        self.save_action.triggered.connect(self._request_save_document)
        self.save_as_action.triggered.connect(self._request_save_as_document)
        self.export_action.triggered.connect(self._request_export_png)
        self.exit_action.triggered.connect(self.close)
        self.zoom_in_action.triggered.connect(self.canvas_view.zoom_in)
        self.zoom_out_action.triggered.connect(self.canvas_view.zoom_out)
        self.fit_action.triggered.connect(self.canvas_view.fit_document)
        self.actual_size_action.triggered.connect(self.canvas_view.actual_size)
        undo_stack = self.controller.commands.undo_stack
        self.undo_action.triggered.connect(lambda _checked=False: undo_stack.undo())
        self.redo_action.triggered.connect(lambda _checked=False: undo_stack.redo())
        undo_stack.canUndoChanged.connect(self._update_undo_actions)
        undo_stack.canRedoChanged.connect(self._update_undo_actions)
        undo_stack.undoTextChanged.connect(self._update_undo_actions)
        undo_stack.redoTextChanged.connect(self._update_undo_actions)
        self.controller.document_changed.connect(self._on_document_changed)
        self.controller.document_event.connect(self._on_document_event)
        self.controller.status_message.connect(self.statusBar().showMessage)
        self.canvas_view.zoom_changed.connect(self._on_zoom_changed)
        self.canvas_view.cursor_document_position_changed.connect(self._on_cursor_position)

    def _make_dock(self, title: str, object_name: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(object_name)
        dock.setProperty("panelRole", "right")
        dock.setStyleSheet(RIGHT_DOCK_STYLE)

        sidebar = QWidget(dock)
        sidebar.setObjectName(f"{object_name}Sidebar")
        sidebar.setProperty("panelRole", "rightSidebar")
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(4, 3, 4, 4)
        sidebar_layout.setSpacing(0)

        card = QFrame(sidebar)
        card.setObjectName(f"{object_name}Card")
        card.setProperty("panelRole", "dockCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(4, 4, 4, 4)
        card_layout.setSpacing(0)
        card_layout.addWidget(widget)
        sidebar_layout.addWidget(card)

        dock.setWidget(sidebar)
        return dock

    def _request_new_document(self) -> None:
        self.controller.request_new_document(self)

    def _request_close_document(self) -> None:
        self.controller.close_document(self)

    def _request_open_document(self) -> None:
        self.controller.request_open_document(self)

    def _request_save_document(self) -> None:
        self.controller.request_save(self)

    def _request_save_as_document(self) -> None:
        self.controller.request_save_as(self)

    def _request_export_png(self) -> None:
        self.controller.request_export_png(self)

    def _on_document_changed(self, document: Document | None) -> None:
        self.tool_controller.reset_active_tool()
        self.canvas_view.set_document(document)
        self.layer_panel.set_document(document)
        has_document = document is not None
        self.central_stack.setCurrentWidget(self.canvas_view if has_document else self.empty_state)
        self.close_action.setEnabled(has_document)
        self.save_action.setEnabled(has_document)
        self.save_as_action.setEnabled(has_document)
        self.export_action.setEnabled(has_document)
        for action in (
            self.zoom_in_action,
            self.zoom_out_action,
            self.fit_action,
            self.actual_size_action,
        ):
            action.setEnabled(has_document)
        for action in self.tool_actions.values():
            action.setEnabled(has_document and not self._editing_busy)
        self.tool_options_panel.setEnabled(has_document and not self._editing_busy)
        if document is None:
            self.document_size_label.setText("无文档")
        else:
            self.document_size_label.setText(f"{document.width} × {document.height} px")
        self._update_undo_actions()
        self._update_window_title()

    def _activate_tool(self, tool_name: str) -> None:
        tool = self._tools[tool_name]
        self.tool_controller.set_tool(tool)
        self.tool_options_panel.show_tool(tool_name)

    def _foreground_rgba(self) -> tuple[int, int, int, int]:
        color = self.color_panel.foreground
        return color.red(), color.green(), color.blue(), color.alpha()

    def _set_foreground_rgba(self, color: tuple[int, int, int, int]) -> None:
        self.color_panel.set_foreground(QColor.fromRgb(*color))

    def _set_background_rgba(self, color: tuple[int, int, int, int]) -> None:
        self.color_panel.set_background(QColor.fromRgb(*color))

    def _save_brush_settings(self, settings: object) -> None:
        from easypicture.imaging import BrushSettings

        if isinstance(settings, BrushSettings):
            self.settings.set_brush_settings(settings)
            self.settings.sync()

    def _save_bucket_settings(self, settings: object) -> None:
        from easypicture.tools import BucketSettings

        if isinstance(settings, BucketSettings):
            self.settings.set_bucket_settings(settings)
            self.settings.sync()

    def _save_eyedropper_settings(self, sample_merged: bool) -> None:
        self.settings.set_eyedropper_sample_merged(sample_merged)
        self.settings.sync()

    def _set_editing_busy(self, busy: bool) -> None:
        self._editing_busy = busy
        has_document = self.controller.document is not None
        for action in self.tool_actions.values():
            action.setEnabled(has_document and not busy)
        self.tool_options_panel.setEnabled(has_document and not busy)
        self.layer_panel.setEnabled(not busy)

    def _on_document_event(self, event: DocumentEvent) -> None:
        self.layer_panel.handle_event(event)
        self._update_window_title()

    def _update_undo_actions(self, *_args: object) -> None:
        stack = self.controller.commands.undo_stack
        self.undo_action.setEnabled(stack.canUndo())
        self.redo_action.setEnabled(stack.canRedo())
        undo_text = stack.undoText()
        redo_text = stack.redoText()
        self.undo_action.setText(f"撤销 {undo_text}" if undo_text else "撤销")
        self.redo_action.setText(f"重做 {redo_text}" if redo_text else "重做")

    def _update_window_title(self) -> None:
        document = self.controller.document
        if document is None:
            self.setWindowTitle(f"EasyPicture {__version__}")
            return
        modified_marker = "*" if document.modified else ""
        self.setWindowTitle(f"{document.display_name}{modified_marker} — EasyPicture")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{zoom * 100:.1f}%")

    def _on_cursor_position(self, point: Point | None) -> None:
        if point is None:
            self.coordinate_label.setText("x: —  y: —")
        else:
            self.coordinate_label.setText(f"x: {int(point.x)}  y: {int(point.y)}")

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self.controller.request_exit(self):
            event.ignore()
            return
        self.settings.save_window(self)
        event.accept()


__all__ = ["EmptyStateWidget", "MainWindow"]
