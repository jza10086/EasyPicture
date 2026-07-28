"""P2 single-document main window with layer management and undo."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QColor, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFrame,
    QLabel,
    QMainWindow,
    QProgressDialog,
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
from easypicture.imaging import (
    BACKGROUND_SELECTION_PIXEL_THRESHOLD,
    SelectionAdjustment,
    SelectionAdjustmentRequest,
    SelectionBulkOperation,
    SelectionBulkRequest,
    SelectionCombineMode,
    SelectionOperationResult,
    calculate_bulk_selection,
    calculate_selection_adjustment,
    clear_selection_tiles,
    combine_selection_tiles,
    invert_selection_tiles,
    select_all_candidate,
)
from easypicture.jobs import JobHandle, JobOperation, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.render import RenderBackendKind
from easypicture.tools import (
    BrushTool,
    BucketTool,
    EyedropperTool,
    MagicWandSettings,
    MagicWandTool,
    NullTool,
    RectangleSelectionTool,
    Tool,
    ToolContext,
    ToolController,
    TransformTool,
)
from easypicture.ui.canvas_view import CanvasView
from easypicture.ui.color_panel import ColorPanel
from easypicture.ui.drag_value_spinbox import configure_numeric_drag_sensitivity
from easypicture.ui.layer_panel import LayerPanel
from easypicture.ui.overlays import OverlayManager
from easypicture.ui.preferences_dialog import PreferencesDialog
from easypicture.ui.settings import AppSettings
from easypicture.ui.tool_options import ToolOptionsPanel
from easypicture.ui.transform_properties import TransformPropertiesPanel

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
        *,
        render_backend: RenderBackendKind | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        configure_numeric_drag_sensitivity(
            settings.numeric_drag_sensitivity(),
            settings.numeric_fine_drag_sensitivity(),
        )
        self.controller.set_dialog_parent(self)
        self.setObjectName("MainWindow")
        self.setStyleSheet(RIGHT_SIDEBAR_STYLE)
        self.resize(1100, 720)
        self._editing_busy = False
        self._selection_job: JobHandle | None = None
        self._selection_progress: QProgressDialog | None = None

        self.canvas_view = CanvasView(
            self,
            render_backend=render_backend or self.settings.render_backend(),
        )
        self.canvas_view.set_selection_outline_style(
            self.settings.selection_outline_color(),
            self.settings.selection_outline_width(),
        )
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
        self.preferences_action = QAction("全局设置…", self)
        self.transform_action = QAction("自由变换", self)
        self.transform_action.setShortcut(QKeySequence("Ctrl+T"))
        self.transform_action.setEnabled(False)

        self.zoom_in_action = QAction("放大", self)
        self.zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        self.zoom_out_action = QAction("缩小", self)
        self.zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        self.fit_action = QAction("适合窗口", self)
        self.fit_action.setShortcut(QKeySequence("Ctrl+0"))
        self.actual_size_action = QAction("100%", self)
        self.actual_size_action.setShortcut(QKeySequence("Ctrl+1"))

        self.select_all_action = QAction("全选", self)
        self.select_all_action.setShortcut(QKeySequence("Ctrl+A"))
        self.deselect_action = QAction("取消选择", self)
        self.deselect_action.setShortcut(QKeySequence("Ctrl+D"))
        self.invert_selection_action = QAction("反选", self)
        self.invert_selection_action.setShortcut(QKeySequence("Ctrl+Shift+I"))
        self.feather_selection_action = QAction("羽化…", self)
        self.grow_selection_action = QAction("扩展…", self)
        self.shrink_selection_action = QAction("收缩…", self)
        self.smooth_selection_action = QAction("平滑…", self)

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
        edit_menu.addSeparator()
        edit_menu.addAction(self.transform_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.preferences_action)

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.actual_size_action)

        selection_menu = self.menuBar().addMenu("选择(&S)")
        selection_menu.addAction(self.select_all_action)
        selection_menu.addAction(self.deselect_action)
        selection_menu.addAction(self.invert_selection_action)
        selection_menu.addSeparator()
        selection_menu.addAction(self.feather_selection_action)
        selection_menu.addAction(self.grow_selection_action)
        selection_menu.addAction(self.shrink_selection_action)
        selection_menu.addAction(self.smooth_selection_action)

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
            "rectangle_selection": RectangleSelectionTool(
                self.tool_options_panel.rectangle_selection.mode
            ),
            "magic_wand": MagicWandTool(self.tool_options_panel.magic_wand.settings),
            "transform": TransformTool(
                self.tool_options_panel.transform.interpolation,
                self.tool_options_panel.transform.irregular_edit,
                self.tool_options_panel.transform.set_parameters,
                self._set_transform_corners,
                self._set_transform_pivot,
                self._update_undo_actions,
            ),
        }
        self.tool_actions: dict[str, QAction] = {}
        for name, label, shortcut in (
            ("brush", "画笔", "B"),
            ("eraser", "橡皮", "E"),
            ("eyedropper", "吸管", "I"),
            ("bucket", "油漆桶", "G"),
            ("rectangle_selection", "矩形选区", "M"),
            ("magic_wand", "魔棒", "W"),
            ("transform", "自由变换", "T"),
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
        self.tool_options_panel.selection_mode_changed.connect(self._save_selection_mode)
        self.tool_options_panel.magic_wand_settings_changed.connect(
            self._save_magic_wand_settings
        )
        self.tool_options_panel.deselect_requested.connect(self._deselect)
        self.tool_options_panel.transform_options_changed.connect(
            self._transform_options_changed
        )
        self.tool_options_panel.transform_parameters_changed.connect(
            self._transform_parameters_changed
        )
        self.tool_options_panel.transform_parameter_edit_started.connect(
            self._transform_edit_started
        )
        self.tool_options_panel.transform_parameter_edit_finished.connect(
            self._transform_edit_finished
        )
        self.tool_options_panel.transform_parameter_edit_canceled.connect(
            self._transform_edit_canceled
        )
        self.tool_options_panel.transform_commit_requested.connect(
            self._transform_commit
        )
        self.tool_options_panel.transform_cancel_requested.connect(
            self._transform_cancel
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

        self.properties_stack = QStackedWidget(self)
        properties_placeholder = QLabel("选择自由变换工具以编辑角点坐标", self)
        properties_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        properties_placeholder.setWordWrap(True)
        self.transform_properties = TransformPropertiesPanel(self)
        self.properties_stack.addWidget(properties_placeholder)
        self.properties_stack.addWidget(self.transform_properties)
        self.transform_properties.corners_changed.connect(
            self._transform_corners_changed
        )
        self.transform_properties.pivot_changed.connect(
            self._transform_pivot_changed
        )
        self.transform_properties.edit_started.connect(self._transform_edit_started)
        self.transform_properties.edit_finished.connect(self._transform_edit_finished)
        self.transform_properties.edit_canceled.connect(self._transform_edit_canceled)
        self.properties_dock = self._make_dock(
            "属性",
            "PropertiesDock",
            self.properties_stack,
        )
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
        self.preferences_action.triggered.connect(self._show_preferences)
        self.transform_action.triggered.connect(self._activate_transform_command)
        self.zoom_in_action.triggered.connect(self.canvas_view.zoom_in)
        self.zoom_out_action.triggered.connect(self.canvas_view.zoom_out)
        self.fit_action.triggered.connect(self.canvas_view.fit_document)
        self.actual_size_action.triggered.connect(self.canvas_view.actual_size)
        self.select_all_action.triggered.connect(self._select_all)
        self.deselect_action.triggered.connect(self._deselect)
        self.invert_selection_action.triggered.connect(self._invert_selection)
        self.feather_selection_action.triggered.connect(
            lambda _checked=False: self._adjust_selection(
                SelectionAdjustment.FEATHER,
                "羽化选区",
            )
        )
        self.grow_selection_action.triggered.connect(
            lambda _checked=False: self._adjust_selection(
                SelectionAdjustment.GROW,
                "扩展选区",
            )
        )
        self.shrink_selection_action.triggered.connect(
            lambda _checked=False: self._adjust_selection(
                SelectionAdjustment.SHRINK,
                "收缩选区",
            )
        )
        self.smooth_selection_action.triggered.connect(
            lambda _checked=False: self._adjust_selection(
                SelectionAdjustment.SMOOTH,
                "平滑选区",
            )
        )
        undo_stack = self.controller.commands.undo_stack
        self.undo_action.triggered.connect(self._undo_requested)
        self.redo_action.triggered.connect(self._redo_requested)
        undo_stack.canUndoChanged.connect(self._update_undo_actions)
        undo_stack.canRedoChanged.connect(self._update_undo_actions)
        undo_stack.undoTextChanged.connect(self._update_undo_actions)
        undo_stack.redoTextChanged.connect(self._update_undo_actions)
        self.controller.document_changed.connect(self._on_document_changed)
        self.controller.document_event.connect(self._on_document_event)
        self.controller.status_message.connect(self.statusBar().showMessage)
        self.canvas_view.render_backend_fallback.connect(
            lambda reason: self.statusBar().showMessage(
                f"OpenGL 渲染不可用，已回退到软件后端：{reason}",
                10_000,
            )
        )
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
        if self._selection_job is not None:
            self._selection_job.cancel()
            self._selection_job = None
            self._close_selection_progress()
            self._editing_busy = False
        self.canvas_view.set_document(document)
        self.tool_controller.reset_active_tool()
        self.layer_panel.set_document(document)
        has_document = document is not None
        self.central_stack.setCurrentWidget(self.canvas_view if has_document else self.empty_state)
        self.close_action.setEnabled(has_document)
        self.save_action.setEnabled(has_document)
        self.save_as_action.setEnabled(has_document)
        self.export_action.setEnabled(has_document)
        self.transform_action.setEnabled(has_document and not self._editing_busy)
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
        self._update_selection_actions()
        self._update_window_title()

    def _activate_tool(self, tool_name: str) -> None:
        tool = self._tools[tool_name]
        if tool is self.tool_controller.active_tool and isinstance(tool, TransformTool):
            tool.restart_session()
        else:
            self.tool_controller.set_tool(tool)
        self.tool_options_panel.show_tool(tool_name)
        if tool_name == "transform" and hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentWidget(self.transform_properties)
            self.properties_dock.show()
            self.properties_dock.raise_()
        elif hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentIndex(0)

    def _activate_transform_command(self) -> None:
        if self.controller.document is None or self._editing_busy:
            return
        self.tool_actions["transform"].setChecked(True)
        self._activate_tool("transform")

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

    def _save_selection_mode(self, mode: object) -> None:
        if isinstance(mode, SelectionCombineMode):
            self.settings.set_selection_combine_mode(mode)
            self.settings.sync()

    def _save_magic_wand_settings(self, settings: object) -> None:
        if isinstance(settings, MagicWandSettings):
            self.settings.set_magic_wand_settings(settings)
            self.settings.sync()

    def _transform_options_changed(self) -> None:
        options = self.tool_options_panel.transform
        self.settings.set_transform_options(
            options.interpolation(),
            options.irregular_edit(),
        )
        self.settings.sync()
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.refresh_options()

    def _transform_parameters_changed(self, values: object) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.apply_numeric_parameters(values)

    def _set_transform_corners(self, values: object) -> None:
        if values is None or (
            isinstance(values, tuple)
            and len(values) == 8
            and all(isinstance(value, (int, float)) for value in values)
        ):
            self.transform_properties.set_corners(values)

    def _set_transform_pivot(self, values: object) -> None:
        if values is None or (
            isinstance(values, tuple)
            and len(values) == 2
            and all(isinstance(value, (int, float)) for value in values)
        ):
            self.transform_properties.set_pivot(values)

    def _transform_corners_changed(self, values: object) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.apply_corner_values(values)

    def _transform_pivot_changed(self, values: object) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.apply_pivot_values(values)

    def _transform_edit_started(self, label: str) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.begin_preview_edit(label)

    def _transform_edit_finished(self) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.finish_preview_edit()

    def _transform_edit_canceled(self) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.cancel_preview_edit()

    def _transform_commit(self) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.commit()

    def _transform_cancel(self) -> None:
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool):
            tool.cancel()

    def _show_preferences(self) -> None:
        dialog = PreferencesDialog(
            self.settings.selection_outline_color(),
            self.settings.selection_outline_width(),
            self.settings.render_backend(),
            self.canvas_view.render_coordinator.diagnostics,
            self,
            numeric_drag_sensitivity=self.settings.numeric_drag_sensitivity(),
            numeric_fine_drag_sensitivity=(
                self.settings.numeric_fine_drag_sensitivity()
            ),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.settings.set_selection_outline_style(
            dialog.selection_color,
            dialog.selection_width,
        )
        self.settings.set_render_backend(dialog.render_backend)
        self.settings.set_numeric_drag_sensitivity(
            dialog.numeric_drag_sensitivity,
            dialog.numeric_fine_drag_sensitivity,
        )
        self.settings.sync()
        configure_numeric_drag_sensitivity(
            dialog.numeric_drag_sensitivity,
            dialog.numeric_fine_drag_sensitivity,
        )
        self.canvas_view.set_selection_outline_style(
            dialog.selection_color,
            dialog.selection_width,
        )

    def _set_editing_busy(self, busy: bool) -> None:
        self._editing_busy = busy
        has_document = self.controller.document is not None
        for action in self.tool_actions.values():
            action.setEnabled(has_document and not busy)
        self.tool_options_panel.setEnabled(has_document and not busy)
        self.layer_panel.setEnabled(not busy)
        self.transform_action.setEnabled(has_document and not busy)
        self._update_selection_actions()

    def _on_document_event(self, event: DocumentEvent) -> None:
        self.layer_panel.handle_event(event)
        self._update_window_title()
        self._update_selection_actions()

    def _select_all(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        if document.width * document.height > BACKGROUND_SELECTION_PIXEL_THRESHOLD:
            request = SelectionBulkRequest(
                DocumentSnapshot.capture(document),
                SelectionBulkOperation.SELECT_ALL,
            )
            self._start_selection_job(
                lambda token, progress: calculate_bulk_selection(
                    request,
                    token,
                    progress,
                ),
                "全选",
            )
            return
        candidate = select_all_candidate(document.size)
        updates = combine_selection_tiles(
            document.selection.mask,
            candidate,
            document.size,
            SelectionCombineMode.REPLACE,
        )
        self.controller.commands.apply_selection_tiles(updates, "全选")

    def _deselect(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        self.controller.commands.apply_selection_tiles(
            clear_selection_tiles(document.selection.mask),
            "取消选择",
        )

    def _invert_selection(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy or not document.selection.is_active:
            return
        if document.width * document.height > BACKGROUND_SELECTION_PIXEL_THRESHOLD:
            request = SelectionBulkRequest(
                DocumentSnapshot.capture(document),
                SelectionBulkOperation.INVERT,
            )
            self._start_selection_job(
                lambda token, progress: calculate_bulk_selection(
                    request,
                    token,
                    progress,
                ),
                "反选",
            )
            return
        self.controller.commands.apply_selection_tiles(
            invert_selection_tiles(document.selection.mask, document.size),
            "反选",
        )

    def _adjust_selection(
        self,
        operation: SelectionAdjustment,
        label: str,
    ) -> None:
        document = self.controller.document
        if (
            document is None
            or self._editing_busy
            or not document.selection.is_active
            or self._selection_job is not None
        ):
            return
        maximum = 1024 if operation in {
            SelectionAdjustment.GROW,
            SelectionAdjustment.SHRINK,
        } else 256
        radius = self.controller.dialogs.request_selection_radius(
            self,
            label,
            maximum,
        )
        if radius is None:
            return
        request = SelectionAdjustmentRequest(
            DocumentSnapshot.capture(document),
            operation,
            radius,
        )
        self._start_selection_job(
            lambda token, progress: calculate_selection_adjustment(
                request,
                token,
                progress,
            ),
            label,
        )

    def _start_selection_job(
        self,
        operation: JobOperation,
        label: str,
    ) -> None:
        if self._selection_job is not None:
            return
        handle = self.controller.jobs.submit(operation)
        self._selection_job = handle
        dialog = QProgressDialog(f"正在{label}…", "取消", 0, 0, self)
        dialog.setWindowTitle(label)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(150)
        dialog.setAutoClose(False)
        dialog.canceled.connect(handle.cancel)
        handle.progress.connect(
            lambda value, maximum_value, message: self._update_selection_progress(
                dialog,
                value,
                maximum_value,
                message,
            )
        )
        self._selection_progress = dialog
        self._set_editing_busy(True)
        handle.finished.connect(lambda outcome: self._on_selection_adjusted(outcome, label))

    @staticmethod
    def _update_selection_progress(
        dialog: QProgressDialog,
        value: int,
        maximum: int,
        message: str,
    ) -> None:
        dialog.setLabelText(message)
        dialog.setMaximum(maximum)
        dialog.setValue(value)

    def _on_selection_adjusted(self, outcome: JobOutcome, label: str) -> None:
        self._close_selection_progress()
        self._selection_job = None
        self._set_editing_busy(False)
        if outcome.status is JobStatus.CANCELLED:
            self.statusBar().showMessage(f"已取消{label}")
            return
        if outcome.status is JobStatus.FAILED:
            self.statusBar().showMessage(f"{label}失败：{outcome.error}")
            return
        if not isinstance(outcome.value, SelectionOperationResult):
            self.statusBar().showMessage(f"{label}失败：无效的任务结果")
            return
        result = outcome.value
        document = self.controller.document
        if (
            document is None
            or document.id != result.document_id
            or document.revision != result.revision
        ):
            self.statusBar().showMessage(f"文档已变化，已丢弃过期{label}结果")
            return
        if not self.controller.commands.apply_selection_tiles(result.updates, label):
            self.statusBar().showMessage(f"{label}未产生变化")

    def _close_selection_progress(self) -> None:
        if self._selection_progress is not None:
            self._selection_progress.close()
            self._selection_progress.deleteLater()
            self._selection_progress = None

    def _update_selection_actions(self) -> None:
        document = self.controller.document
        has_document = document is not None
        active = has_document and document.selection.is_active if document is not None else False
        self.select_all_action.setEnabled(has_document and not self._editing_busy)
        self.tool_options_panel.set_selection_active(active and not self._editing_busy)
        for action in (
            self.deselect_action,
            self.invert_selection_action,
            self.feather_selection_action,
            self.grow_selection_action,
            self.shrink_selection_action,
            self.smooth_selection_action,
        ):
            action.setEnabled(active and not self._editing_busy)

    def _update_undo_actions(self, *_args: object) -> None:
        tool = self._tools.get("transform") if hasattr(self, "_tools") else None
        if isinstance(tool, TransformTool) and tool.has_active_session:
            undo_text = tool.preview_undo_text
            redo_text = tool.preview_redo_text
            self.undo_action.setEnabled(tool.can_undo_preview)
            self.redo_action.setEnabled(tool.can_redo_preview)
            self.undo_action.setText(
                f"撤销 {undo_text}" if undo_text else "撤销"
            )
            self.redo_action.setText(
                f"重做 {redo_text}" if redo_text else "重做"
            )
            return
        stack = self.controller.commands.undo_stack
        self.undo_action.setEnabled(stack.canUndo())
        self.redo_action.setEnabled(stack.canRedo())
        undo_text = stack.undoText()
        redo_text = stack.redoText()
        self.undo_action.setText(f"撤销 {undo_text}" if undo_text else "撤销")
        self.redo_action.setText(f"重做 {redo_text}" if redo_text else "重做")

    def _undo_requested(self, _checked: bool = False) -> None:
        _ = _checked
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool) and tool.has_active_session:
            tool.undo_preview()
            return
        self.controller.commands.undo_stack.undo()

    def _redo_requested(self, _checked: bool = False) -> None:
        _ = _checked
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool) and tool.has_active_session:
            tool.redo_preview()
            return
        self.controller.commands.undo_stack.redo()

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
