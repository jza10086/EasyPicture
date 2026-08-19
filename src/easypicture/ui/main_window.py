"""Main window for the multi-project EasyPicture workspace."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from PySide6.QtCore import QEvent, QModelIndex, QObject, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFontDatabase,
    QKeyEvent,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTabBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from easypicture import __version__
from easypicture.domain import (
    AffineTransform,
    Document,
    DocumentEvent,
    DocumentKind,
    GroupDeleteMode,
    GroupListChanged,
    GroupMembersChanged,
    PixelPoint,
    Point,
    Size,
    SpriteFrameContentChanged,
    SpriteFrameContentMode,
    SpriteWorkspaceChanged,
    SvgElementObject,
    TextObject,
    parse_editable_svg,
)
from easypicture.imaging import (
    BACKGROUND_SELECTION_PIXEL_THRESHOLD,
    BrightnessContrastAdjustment,
    CanvasResizeRequest,
    ColorAdjustmentKind,
    ColorReplaceAdjustment,
    CropDocumentRequest,
    CurvesAdjustment,
    DocumentGeometryResult,
    GammaAdjustment,
    GeometryRect,
    GradientMapAdjustment,
    HslAdjustment,
    ImageScaleRequest,
    InvertAdjustment,
    LayerScaleRequest,
    PaletteMapAdjustment,
    SelectionAdjustment,
    SelectionAdjustmentRequest,
    SelectionBulkOperation,
    SelectionBulkRequest,
    SelectionCombineMode,
    SelectionOperationResult,
    ShapeGeometry,
    ShapeKind,
    ShapeOutputTarget,
    ShapeSettings,
    ThresholdAdjustment,
    TrimTransparentRequest,
    calculate_bulk_selection,
    calculate_canvas_resize,
    calculate_crop_document,
    calculate_image_scale,
    calculate_layer_scale,
    calculate_selection_adjustment,
    calculate_trim_transparent,
    canvas_anchor_offset,
    clear_selection_tiles,
    combine_selection_tiles,
    invert_selection_tiles,
    select_all_candidate,
    selection_bounds,
)
from easypicture.jobs import JobHandle, JobOperation, JobOutcome, JobStatus
from easypicture.persistence import DocumentSnapshot
from easypicture.pixels import Rect
from easypicture.render import RenderBackendKind
from easypicture.runtime import RuntimePaths
from easypicture.tools import (
    BrushTool,
    BucketTool,
    CropTool,
    EyedropperTool,
    MagicWandSettings,
    MagicWandTool,
    NullTool,
    RectangleSelectionTool,
    ShapeTool,
    SpriteSliceTool,
    TextTool,
    TextTransformTool,
    Tool,
    ToolContext,
    ToolController,
    TransformTool,
)
from easypicture.ui.about_dialog import AboutDialog
from easypicture.ui.atlas_export_dialog import AtlasExportDialog
from easypicture.ui.canvas_view import CanvasView
from easypicture.ui.color_adjustment_controller import ColorAdjustmentController
from easypicture.ui.color_adjustment_panel import ColorAdjustmentPanel
from easypicture.ui.color_panel import ColorPanel
from easypicture.ui.drag_value_spinbox import configure_numeric_drag_sensitivity
from easypicture.ui.geometry_dialogs import CanvasSizeDialog, ScaleGeometryDialog
from easypicture.ui.layer_panel import LayerPanel
from easypicture.ui.notifications import NoticeSeverity, UserNotice
from easypicture.ui.overlays import OverlayManager
from easypicture.ui.preferences_dialog import PreferencesDialog
from easypicture.ui.settings import AppSettings
from easypicture.ui.shape_properties import ShapePropertiesPanel
from easypicture.ui.sprite_frame_panel import SpriteFramePanel
from easypicture.ui.sprite_workspace_panel import SpriteWorkspacePanel
from easypicture.ui.svg_properties import SvgPropertiesPanel
from easypicture.ui.text_properties import TextPropertiesPanel
from easypicture.ui.tool_options import ToolOptionsPanel
from easypicture.ui.transform_properties import TransformPropertiesPanel
from easypicture.workspace import CanvasViewState, EditingSurfaceRef, WorkspaceController

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
        self.open_button = QPushButton("打开项目", self)
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
        controller: WorkspaceController,
        settings: AppSettings,
        parent: QWidget | None = None,
        *,
        render_backend: RenderBackendKind | None = None,
        runtime_paths: RuntimePaths | None = None,
    ) -> None:
        super().__init__(parent)
        self.controller = controller
        self.settings = settings
        self.runtime_paths = runtime_paths if runtime_paths is not None else RuntimePaths.from_qt()
        configure_numeric_drag_sensitivity(
            settings.numeric_drag_sensitivity(),
            settings.numeric_fine_drag_sensitivity(),
        )
        self.controller.set_dialog_parent(self)
        self.setObjectName("MainWindow")
        self.setStyleSheet(RIGHT_SIDEBAR_STYLE)
        self.setMinimumSize(800, 600)
        self.resize(1100, 720)
        self._editing_busy = False
        self._selected_vector_object = False
        self._warned_text_fonts: set[str] = set()
        self._group_expansion_state: dict[UUID, bool] = {}
        self._pending_group_expansion: dict[UUID, bool] | None = None
        self._group_expansion_restore_scheduled = False
        self._selection_job: JobHandle | None = None
        self._selection_progress: QProgressDialog | None = None
        self._geometry_job: JobHandle | None = None
        self._geometry_progress: QProgressDialog | None = None
        self._adjustment_previous_tool: Tool | None = None
        self._sprite_editing_frame_id: UUID | None = None
        self._geometry_clear_sprite_frames = False

        self.canvas_view = CanvasView(
            self,
            render_backend=render_backend or self.settings.render_backend(),
        )
        self.canvas_view.set_selection_outline_style(
            self.settings.selection_outline_color(),
            self.settings.selection_outline_width(),
        )
        self.canvas_view.set_sprite_grid_visibility(
            pixel_grid=self.settings.pixel_grid_visible(),
            cell_grid=self.settings.sprite_grid_visible(),
        )
        self.empty_state = EmptyStateWidget(self)
        self.central_stack = QStackedWidget(self)
        self.central_stack.addWidget(self.empty_state)
        self.central_stack.addWidget(self.canvas_view)
        self.workspace_tabs = QTabBar(self)
        self.workspace_tabs.setMovable(True)
        self.workspace_tabs.setTabsClosable(True)
        self.workspace_tabs.setExpanding(False)
        self.workspace_tabs.currentChanged.connect(self._workspace_tab_changed)
        self.workspace_tabs.tabCloseRequested.connect(self._workspace_tab_close_requested)
        self.workspace_add_button = QPushButton("+", self)
        self.workspace_add_button.setFixedWidth(34)
        self.workspace_add_button.clicked.connect(lambda: self._request_new_document())
        project_tab_row = QWidget(self)
        project_tab_layout = QHBoxLayout(project_tab_row)
        project_tab_layout.setContentsMargins(0, 0, 0, 0)
        project_tab_layout.setSpacing(1)
        project_tab_layout.addWidget(self.workspace_tabs, 1)
        project_tab_layout.addWidget(self.workspace_add_button)
        self.surface_tabs = QTabBar(self)
        self.surface_tabs.setExpanding(False)
        self.surface_tabs.setTabsClosable(True)
        self.surface_tabs.currentChanged.connect(self._surface_tab_changed)
        self.surface_tabs.tabCloseRequested.connect(self._surface_tab_close_requested)
        central_container = QWidget(self)
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(project_tab_row)
        central_layout.addWidget(self.surface_tabs)
        central_layout.addWidget(self.central_stack, 1)
        self.setCentralWidget(central_container)

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
            selected_node_provider=lambda: (
                self.layer_panel.current_node() if hasattr(self, "layer_panel") else None
            ),
            select_node=lambda node: (
                self.layer_panel.select_node(node) if hasattr(self, "layer_panel") else None
            ),
        )
        self.tool_controller = ToolController(self.tool_context, NullTool())
        self.canvas_view.set_tool_controller(self.tool_controller)

        self._create_actions()
        self._create_menus()
        self._create_toolbars()
        self._create_docks()
        self._color_apply_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self._color_apply_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._color_apply_shortcut.activated.connect(self._apply_color_adjustment_if_active)
        self._create_status_bar()
        self._connect_signals()
        self._on_document_changed(self.controller.document)
        self._refresh_workspace_tabs()
        self.settings.restore_window(self)
        self.setCorner(
            Qt.Corner.BottomRightCorner,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )

    def _create_actions(self) -> None:
        self.new_action = QAction("新建(&N)", self)
        self.new_action.setShortcut(QKeySequence.StandardKey.New)
        self.open_action = QAction("打开(&O)", self)
        self.open_action.setShortcut(QKeySequence.StandardKey.Open)
        self.import_action = QAction("导入…", self)
        self.append_action = QAction("追加…", self)
        self.append_action.setEnabled(False)
        self.close_action = QAction("关闭文档(&C)", self)
        self.close_action.setShortcut(QKeySequence.StandardKey.Close)
        self.exit_action = QAction("退出(&X)", self)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)

        self.save_action = QAction("保存(&S)", self)
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_as_action = QAction("另存为", self)
        self.export_action = QAction("导出图片…", self)
        self.export_sprite_frame_action = QAction("导出当前帧 PNG…", self)
        self.export_sprite_atlas_action = QAction("导出完整精灵图…", self)
        for action in (
            self.save_action,
            self.save_as_action,
            self.export_action,
            self.export_sprite_frame_action,
            self.export_sprite_atlas_action,
        ):
            action.setEnabled(False)

        self.pixel_exact_action = QAction("像素精确坐标", self)
        self.pixel_exact_action.setCheckable(True)
        self.pixel_exact_action.setChecked(self.settings.pixel_exact_coordinates())
        self.pixel_grid_action = QAction("显示像素网格", self)
        self.pixel_grid_action.setCheckable(True)
        self.pixel_grid_action.setChecked(self.settings.pixel_grid_visible())
        self.sprite_grid_action = QAction("显示单格网格", self)
        self.sprite_grid_action.setCheckable(True)
        self.sprite_grid_action.setChecked(self.settings.sprite_grid_visible())
        self.sprite_snap_action = QAction("吸附到单格网格", self)
        self.sprite_snap_action.setCheckable(True)
        self.sprite_snap_action.setChecked(self.settings.sprite_grid_snap_enabled())

        self.undo_action = QAction("撤销", self)
        self.redo_action = QAction("重做", self)
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.undo_action.setEnabled(False)
        self.redo_action.setEnabled(False)
        self.preferences_action = QAction("全局设置…", self)
        self.open_log_folder_action = QAction("打开日志目录", self)
        self.about_action = QAction("关于 EasyPicture…", self)
        self.transform_action = QAction("自由变换", self)
        self.transform_action.setShortcut(QKeySequence("Ctrl+T"))
        self.transform_action.setEnabled(False)
        self.canvas_size_action = QAction("画布大小…", self)
        self.image_scale_action = QAction("缩放图像…", self)
        self.crop_to_selection_action = QAction("裁切到选区", self)
        self.trim_transparent_action = QAction("裁切透明像素", self)
        self.layer_scale_action = QAction("缩放图层…", self)
        for action in (
            self.canvas_size_action,
            self.image_scale_action,
            self.crop_to_selection_action,
            self.trim_transparent_action,
            self.layer_scale_action,
        ):
            action.setEnabled(False)

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
        self.clear_selected_pixels_action = QAction("清除选区像素", self)
        self.clear_selected_pixels_action.setShortcut(QKeySequence("Del"))
        self.color_adjustment_actions: dict[ColorAdjustmentKind, QAction] = {}
        for kind, label in (
            (ColorAdjustmentKind.INVERT, "反相"),
            (ColorAdjustmentKind.THRESHOLD, "阈值…"),
            (ColorAdjustmentKind.BRIGHTNESS_CONTRAST, "亮度/对比度…"),
            (ColorAdjustmentKind.GAMMA, "伽马…"),
            (ColorAdjustmentKind.CURVES, "曲线…"),
            (ColorAdjustmentKind.HSL, "色相/饱和度/明度…"),
            (ColorAdjustmentKind.GRADIENT_MAP, "渐变映射…"),
            (ColorAdjustmentKind.COLOR_REPLACE, "颜色替换…"),
            (ColorAdjustmentKind.PALETTE_MAP, "调色板映射…"),
        ):
            action = QAction(label, self)
            action.setEnabled(False)
            if kind is ColorAdjustmentKind.INVERT:
                action.setShortcut(QKeySequence("Ctrl+I"))
            action.triggered.connect(
                lambda _checked=False, adjustment_kind=kind: self._start_color_adjustment(
                    adjustment_kind
                )
            )
            self.color_adjustment_actions[kind] = action

    def _create_menus(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addAction(self.new_action)
        file_menu.addAction(self.open_action)
        file_menu.addAction(self.import_action)
        file_menu.addAction(self.append_action)
        self.recent_files_menu = file_menu.addMenu("最近文件")
        self.recent_files_menu.aboutToShow.connect(self._rebuild_recent_files_menu)
        file_menu.addAction(self.close_action)
        file_menu.addSeparator()
        file_menu.addAction(self.save_action)
        file_menu.addAction(self.save_as_action)
        file_menu.addAction(self.export_action)
        file_menu.addAction(self.export_sprite_frame_action)
        file_menu.addAction(self.export_sprite_atlas_action)
        file_menu.addSeparator()
        file_menu.addAction(self.exit_action)

        edit_menu = self.menuBar().addMenu("编辑(&E)")
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.transform_action)
        edit_menu.addAction(self.clear_selected_pixels_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self.preferences_action)

        image_menu = self.menuBar().addMenu("图像(&I)")
        image_menu.addAction(self.canvas_size_action)
        image_menu.addAction(self.image_scale_action)
        image_menu.addSeparator()
        image_menu.addAction(self.crop_to_selection_action)
        image_menu.addAction(self.trim_transparent_action)

        layer_menu = self.menuBar().addMenu("图层(&L)")
        layer_menu.addAction(self.layer_scale_action)

        color_menu = self.menuBar().addMenu("颜色(&C)")
        for kind in ColorAdjustmentKind:
            color_menu.addAction(self.color_adjustment_actions[kind])
            if kind in (
                ColorAdjustmentKind.INVERT,
                ColorAdjustmentKind.CURVES,
                ColorAdjustmentKind.HSL,
                ColorAdjustmentKind.COLOR_REPLACE,
            ):
                color_menu.addSeparator()

        view_menu = self.menuBar().addMenu("视图(&V)")
        view_menu.addAction(self.zoom_in_action)
        view_menu.addAction(self.zoom_out_action)
        view_menu.addAction(self.fit_action)
        view_menu.addAction(self.actual_size_action)
        view_menu.addSeparator()
        view_menu.addAction(self.pixel_exact_action)
        view_menu.addAction(self.pixel_grid_action)
        view_menu.addAction(self.sprite_grid_action)
        view_menu.addAction(self.sprite_snap_action)

        selection_menu = self.menuBar().addMenu("选择(&S)")
        selection_menu.addAction(self.select_all_action)
        selection_menu.addAction(self.deselect_action)
        selection_menu.addAction(self.invert_selection_action)
        selection_menu.addSeparator()
        selection_menu.addAction(self.feather_selection_action)
        selection_menu.addAction(self.grow_selection_action)
        selection_menu.addAction(self.shrink_selection_action)
        selection_menu.addAction(self.smooth_selection_action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        help_menu.addAction(self.open_log_folder_action)
        help_menu.addSeparator()
        help_menu.addAction(self.about_action)

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
            "crop": CropTool(
                self.tool_options_panel.crop.ratio,
                self.tool_options_panel.crop.show_grid,
                self._set_crop_rect,
                self._commit_crop_rect,
                self._crop_cancel,
            ),
            "transform": TransformTool(
                self.tool_options_panel.transform.interpolation,
                self.tool_options_panel.transform.irregular_edit,
                self.tool_options_panel.transform.set_parameters,
                self._set_transform_corners,
                self._set_transform_pivot,
                self._update_undo_actions,
                self.settings.pixel_exact_coordinates,
            ),
            "shape": ShapeTool(
                self.tool_options_panel.shape.kind,
                self.tool_options_panel.shape.settings,
                self.tool_options_panel.shape.output_target,
                self._set_shape_geometry,
                self._update_undo_actions,
                lambda vector_object: (
                    self.svg_properties.set_preview_object(vector_object)
                    if hasattr(self, "svg_properties")
                    else None
                ),
                self.settings.pixel_exact_coordinates,
            ),
            "text": TextTool(),
            "sprite_slice": SpriteSliceTool(
                self._set_sprite_region,
                self._confirm_sprite_region,
                self._cancel_sprite_slice,
                self.settings.sprite_grid_snap_enabled,
            ),
            "text_transform": TextTransformTool(
                lambda: self.layer_panel.current_node() if hasattr(self, "layer_panel") else None,
                lambda text_object: self.text_properties.set_preview_object(text_object),
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
            ("crop", "裁切", "C"),
            ("transform", "自由变换", "T"),
            ("shape", "图形", "U"),
            ("text", "文字", ""),
            ("sprite_slice", "精灵切片", ""),
        ):
            action = QAction(label, self)
            action.setCheckable(True)
            if shortcut:
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
        self.tool_options_panel.eyedropper_settings_changed.connect(self._save_eyedropper_settings)
        self.tool_options_panel.selection_mode_changed.connect(self._save_selection_mode)
        self.tool_options_panel.magic_wand_settings_changed.connect(self._save_magic_wand_settings)
        self.tool_options_panel.deselect_requested.connect(self._deselect)
        self.tool_options_panel.transform_options_changed.connect(self._transform_options_changed)
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
        self.tool_options_panel.transform_commit_requested.connect(self._transform_commit)
        self.tool_options_panel.transform_cancel_requested.connect(self._transform_cancel)
        self.tool_options_panel.crop_rect_changed.connect(self._crop_rect_changed)
        self.tool_options_panel.crop_options_changed.connect(self._crop_options_changed)
        self.tool_options_panel.crop_commit_requested.connect(self._crop_commit)
        self.tool_options_panel.crop_cancel_requested.connect(self._crop_cancel)
        self.tool_options_panel.shape_kind_changed.connect(self._shape_kind_changed)
        self.tool_options_panel.shape_settings_changed.connect(self._shape_settings_changed)
        self.tool_options_panel.shape_output_target_changed.connect(
            self._shape_output_target_changed
        )
        self.tool_options_panel.shape_commit_requested.connect(self._shape_commit)
        self.tool_options_panel.shape_cancel_requested.connect(self._shape_cancel)
        self.tool_actions["brush"].setChecked(True)
        self._activate_tool("brush")

    def _create_docks(self) -> None:
        self.layer_panel = LayerPanel(self.controller.commands, self)
        self.layer_panel.tree_view.expanded.connect(
            lambda index: self._set_group_expanded(index, True)
        )
        self.layer_panel.tree_view.collapsed.connect(
            lambda index: self._set_group_expanded(index, False)
        )
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
        self.shape_properties = ShapePropertiesPanel(self)
        self.color_adjustment_panel = ColorAdjustmentPanel(
            self.settings,
            self.controller.dialogs,
            self._foreground_rgba,
            lambda: (
                self.color_panel.background.red(),
                self.color_panel.background.green(),
                self.color_panel.background.blue(),
                self.color_panel.background.alpha(),
            ),
            self,
        )
        self.properties_stack.addWidget(properties_placeholder)
        self.properties_stack.addWidget(self.transform_properties)
        self.properties_stack.addWidget(self.shape_properties)
        self.text_properties = TextPropertiesPanel(self.controller.commands, self)
        self.properties_stack.addWidget(self.text_properties)
        self.svg_properties = SvgPropertiesPanel(self.controller.commands, self)
        self.properties_stack.addWidget(self.svg_properties)
        self.sprite_properties = SpriteWorkspacePanel(self)
        self.properties_stack.addWidget(self.sprite_properties)
        self.properties_stack.addWidget(self.color_adjustment_panel)
        self.color_adjustment_controller = ColorAdjustmentController(
            self.canvas_view,
            lambda: self.controller.document,
            self.controller.commands,
            self.controller.jobs,
            self._show_operation_status,
            self,
        )
        self.transform_properties.corners_changed.connect(self._transform_corners_changed)
        self.transform_properties.pivot_changed.connect(self._transform_pivot_changed)
        self.transform_properties.edit_started.connect(self._transform_edit_started)
        self.transform_properties.edit_finished.connect(self._transform_edit_finished)
        self.transform_properties.edit_canceled.connect(self._transform_edit_canceled)
        self.shape_properties.geometry_changed.connect(self._shape_geometry_changed)
        self.shape_properties.edit_started.connect(self._shape_edit_started)
        self.shape_properties.edit_finished.connect(self._shape_edit_finished)
        self.shape_properties.edit_canceled.connect(self._shape_edit_canceled)
        self.svg_properties.preview_changed.connect(self._svg_preview_changed)
        self.svg_properties.preview_cleared.connect(self.canvas_view.clear_vector_preview)
        self.color_adjustment_panel.parameters_changed.connect(
            self._color_adjustment_parameters_changed
        )
        self.color_adjustment_panel.apply_requested.connect(self._apply_color_adjustment)
        self.color_adjustment_panel.cancel_requested.connect(
            self.color_adjustment_controller.cancel
        )
        self.color_adjustment_panel.preview_toggled.connect(
            self.color_adjustment_controller.set_preview_enabled
        )
        self.color_adjustment_panel.sample_requested.connect(
            lambda merged: self.color_adjustment_controller.begin_sampling(merged=merged)
        )
        self.color_adjustment_controller.active_changed.connect(self._on_color_adjustment_active)
        self.color_adjustment_controller.progress_changed.connect(
            self.color_adjustment_panel.set_progress
        )
        self.color_adjustment_controller.preview_ready.connect(
            lambda _result: self.color_adjustment_panel.set_running(False)
        )
        self.color_adjustment_controller.sampled_color.connect(
            self.color_adjustment_panel.use_sampled_source
        )
        self.layer_panel.node_selected.connect(self._on_layer_node_selected)
        self.properties_dock = self._make_dock(
            "属性",
            "PropertiesDock",
            self.properties_stack,
        )
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.properties_dock)
        self.tabifyDockWidget(self.color_dock, self.properties_dock)

        self.sprite_frame_panel = SpriteFramePanel(self.settings, self)
        self.sprite_frames_dock = QDockWidget("精灵帧", self)
        self.sprite_frames_dock.setObjectName("SpriteFramesDock")
        self.sprite_frames_dock.setWidget(self.sprite_frame_panel)
        self.sprite_frames_dock.setMinimumHeight(140)
        self.setCorner(
            Qt.Corner.BottomRightCorner,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self.addDockWidget(
            Qt.DockWidgetArea.BottomDockWidgetArea,
            self.sprite_frames_dock,
        )
        self.sprite_frames_dock.hide()

        self.sprite_properties.cell_size_requested.connect(self._set_sprite_cell_size)
        self.sprite_properties.region_requested.connect(self._sprite_region_requested)
        self.sprite_properties.frame_requested.connect(self._update_sprite_frame_details)
        self.sprite_frame_panel.frame_selected.connect(self._select_sprite_frame)
        self.sprite_frame_panel.new_requested.connect(self._new_sprite_frame)
        self.sprite_frame_panel.delete_requested.connect(self._delete_sprite_frame)
        self.sprite_frame_panel.rename_requested.connect(self._rename_sprite_frame)
        self.sprite_frame_panel.move_requested.connect(self._move_sprite_frame)
        self.sprite_frame_panel.reorder_requested.connect(self._reorder_sprite_frames)
        self.sprite_frame_panel.export_requested.connect(self._export_sprite_frame)
        self.sprite_frame_panel.frame_activated.connect(self._open_sprite_frame_surface)
        self.sprite_frame_panel.duplicate_requested.connect(self._duplicate_sprite_frame)
        self.sprite_frame_panel.detach_requested.connect(self._detach_sprite_frame)
        self.sprite_frame_panel.reset_requested.connect(self._reset_sprite_frame)
        self.sprite_frame_panel.project_selected.connect(self._workspace_tab_changed)
        self.sprite_frame_panel.group_selected.connect(self._sprite_group_selected)
        self.sprite_frame_panel.group_create_requested.connect(self._create_sprite_group)
        self.sprite_frame_panel.group_rename_requested.connect(self._rename_sprite_group)
        self.sprite_frame_panel.group_move_requested.connect(self._move_sprite_group)
        self.sprite_frame_panel.group_delete_requested.connect(self._delete_sprite_group)
        self.sprite_frame_panel.frame_group_move_requested.connect(
            self._move_sprite_frame_to_group
        )
        self.sprite_frame_panel.view_size_changed.connect(
            lambda _size: self._resize_sprite_frame_dock()
        )

    def _create_status_bar(self) -> None:
        self._status_message_generation = 0
        self.shortcut_hint_label = QLabel("B 画笔 · 中键/Space 平移 · Ctrl+滚轮缩放", self)
        status_bar = self.statusBar()
        self.operation_status_label = QLabel("就绪", status_bar)
        self.coordinate_label = QLabel("x: —  y: —", self)
        self.document_size_label = QLabel("无文档", self)
        self.sprite_count_label = QLabel("帧 0", self)
        self.zoom_label = QLabel("100%", self)
        self.shortcut_hint_label.setMinimumWidth(260)
        self.operation_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.operation_status_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents,
            True,
        )
        self.operation_status_label.setStyleSheet("background-color: #252525; padding: 0 6px;")
        status_bar.addWidget(self.shortcut_hint_label, 1)
        status_bar.addPermanentWidget(self.document_size_label)
        status_bar.addPermanentWidget(self.sprite_count_label)
        status_bar.addPermanentWidget(self.coordinate_label)
        status_bar.addPermanentWidget(self.zoom_label)
        status_bar.installEventFilter(self)
        QTimer.singleShot(0, self._position_operation_status)

    def _position_operation_status(self) -> None:
        if not hasattr(self, "operation_status_label"):
            return
        status_bar = self.statusBar()
        width = max(180, min(440, status_bar.width() // 3))
        x = (status_bar.width() - width) // 2
        self.operation_status_label.setGeometry(x, 0, width, status_bar.height())
        self.operation_status_label.raise_()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if (
            hasattr(self, "operation_status_label")
            and watched is self.statusBar()
            and event.type()
            in {
                QEvent.Type.Resize,
                QEvent.Type.Show,
                QEvent.Type.LayoutRequest,
            }
        ):
            QTimer.singleShot(0, self._position_operation_status)
        return super().eventFilter(watched, event)

    def _show_operation_status(self, message: str, timeout: int = 0) -> None:
        self._status_message_generation += 1
        generation = self._status_message_generation
        self.operation_status_label.setText(message or "就绪")
        self._position_operation_status()
        if timeout > 0:
            QTimer.singleShot(
                timeout,
                lambda: self._clear_operation_status(generation),
            )

    def _clear_operation_status(self, generation: int) -> None:
        if generation == self._status_message_generation:
            self.operation_status_label.setText("就绪")

    def _update_shortcut_hint(self, tool_name: str) -> None:
        hints = {
            "brush": "B 画笔 · 拖动绘制 · 中键/Space 平移",
            "eraser": "E 橡皮 · 拖动擦除 · 中键/Space 平移",
            "eyedropper": "I 吸管 · 左键前景色 · 右键背景色",
            "bucket": "G 油漆桶 · 左键填充 · Esc 取消任务",
            "rectangle_selection": "M 矩形选区 · Shift 添加 · Alt 减去",
            "magic_wand": "W 魔棒 · Shift 添加 · Alt 减去",
            "crop": "C 裁切 · Enter 应用 · Esc 取消",
            "transform": "T 自由变换 · Enter 应用 · Esc 取消",
            "shape": "U 图形 · Enter 应用 · Esc 取消",
            "text": "文字 · 拖动创建文本框 · 右键取消",
            "sprite_slice": "精灵切片 · 拖动定位 · Enter 确定 · F2 重命名",
        }
        self.shortcut_hint_label.setText(
            hints.get(tool_name, "中键/Space 平移 · Ctrl+滚轮缩放")
        )

    def _connect_signals(self) -> None:
        self.empty_state.new_button.clicked.connect(self._request_new_document)
        self.empty_state.open_button.clicked.connect(self._request_open_document)
        self.new_action.triggered.connect(self._request_new_document)
        self.open_action.triggered.connect(self._request_open_document)
        self.import_action.triggered.connect(self._request_import_document)
        self.append_action.triggered.connect(self._request_append_document)
        self.close_action.triggered.connect(self._request_close_document)
        self.save_action.triggered.connect(self._request_save_document)
        self.save_as_action.triggered.connect(self._request_save_as_document)
        self.export_action.triggered.connect(self._request_export_image)
        self.export_sprite_frame_action.triggered.connect(self._export_selected_sprite_frame)
        self.export_sprite_atlas_action.triggered.connect(self._export_sprite_atlas)
        self.exit_action.triggered.connect(self.close)
        self.preferences_action.triggered.connect(self._show_preferences)
        self.open_log_folder_action.triggered.connect(self._open_log_folder)
        self.about_action.triggered.connect(self._show_about)
        self.transform_action.triggered.connect(self._activate_transform_command)
        self.canvas_size_action.triggered.connect(self._show_canvas_size_dialog)
        self.image_scale_action.triggered.connect(self._show_image_scale_dialog)
        self.crop_to_selection_action.triggered.connect(self._crop_to_selection)
        self.trim_transparent_action.triggered.connect(self._trim_transparent)
        self.layer_scale_action.triggered.connect(self._show_layer_scale_dialog)
        self.zoom_in_action.triggered.connect(self.canvas_view.zoom_in)
        self.zoom_out_action.triggered.connect(self.canvas_view.zoom_out)
        self.fit_action.triggered.connect(self.canvas_view.fit_document)
        self.actual_size_action.triggered.connect(self.canvas_view.actual_size)
        self.pixel_exact_action.toggled.connect(self._set_pixel_exact_coordinates)
        self.pixel_grid_action.toggled.connect(self._set_pixel_grid_visible)
        self.sprite_grid_action.toggled.connect(self._set_sprite_grid_visible)
        self.sprite_snap_action.toggled.connect(self._set_sprite_snap_enabled)
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
        self.clear_selected_pixels_action.triggered.connect(self._clear_selected_pixels)
        undo_stack = self.controller.commands.undo_stack
        self.undo_action.triggered.connect(self._undo_requested)
        self.redo_action.triggered.connect(self._redo_requested)
        undo_stack.canUndoChanged.connect(self._update_undo_actions)
        undo_stack.canRedoChanged.connect(self._update_undo_actions)
        undo_stack.undoTextChanged.connect(self._update_undo_actions)
        undo_stack.redoTextChanged.connect(self._update_undo_actions)
        self.controller.document_changed.connect(self._on_document_changed)
        self.controller.document_event.connect(self._on_document_event)
        self.controller.status_message.connect(self._show_operation_status)
        self.controller.recent_files_changed.connect(self._rebuild_recent_files_menu)
        self.controller.notifications.notice.connect(self._show_user_notice)
        if hasattr(self.controller, "sessions_changed"):
            self.controller.sessions_changed.connect(self._refresh_workspace_tabs)
            self.controller.active_session_changed.connect(self._refresh_workspace_tabs)
            self.controller.surfaces_changed.connect(self._refresh_surface_tabs)
            self.workspace_tabs.tabMoved.connect(self.controller.move_session)
        self.canvas_view.render_backend_fallback.connect(self._on_render_backend_fallback)
        self.canvas_view.render_coordinator.diagnostics_changed.connect(self._on_render_diagnostics)
        self.canvas_view.zoom_changed.connect(self._on_zoom_changed)
        self.canvas_view.cursor_document_position_changed.connect(self._on_cursor_position)

    def _project_document(self) -> Document | None:
        return getattr(self.controller, "project_document", self.controller.document)

    @staticmethod
    def _project_display_label(document: Document) -> str:
        # A project without a .limg path is not yet saved as a project even if
        # its contents are still clean.  Keep lifecycle prompts tied to
        # Document.modified, while the title marker communicates both states.
        unsaved = document.modified or document.project_path is None
        return f"{document.display_name}{'*' if unsaved else ''}"

    def _refresh_workspace_tabs(self, *_args: object) -> None:
        if not hasattr(self.controller, "sessions"):
            self.workspace_tabs.hide()
            self.surface_tabs.hide()
            return
        sessions = self.controller.sessions
        active = self.controller.active_session
        self.workspace_tabs.blockSignals(True)
        try:
            while self.workspace_tabs.count():
                self.workspace_tabs.removeTab(0)
            for session in sessions:
                document = session.document
                index = self.workspace_tabs.addTab(self._project_display_label(document))
                self.workspace_tabs.setTabData(index, session.id)
            if active is not None:
                self.workspace_tabs.setCurrentIndex(sessions.index(active))
        finally:
            self.workspace_tabs.blockSignals(False)
        self.workspace_tabs.setVisible(bool(sessions))
        self.surface_tabs.setVisible(bool(sessions))
        if hasattr(self, "sprite_frame_panel"):
            labels = tuple(
                self._project_display_label(session.document)
                for session in sessions
            )
            active_index = sessions.index(active) if active is not None else -1
            self.sprite_frame_panel.set_projects(labels, active_index)
        self._refresh_surface_tabs()

    def _refresh_surface_tabs(self, *_args: object) -> None:
        session = getattr(self.controller, "active_session", None)
        self.surface_tabs.blockSignals(True)
        try:
            while self.surface_tabs.count():
                self.surface_tabs.removeTab(0)
            if session is None:
                return
            for surface in session.open_surfaces:
                if surface.kind == "source":
                    title = "源图"
                else:
                    assert surface.frame_id is not None
                    try:
                        frame = session.document.sprite_workspace.frame_by_id(surface.frame_id)
                        if frame.content_mode is SpriteFrameContentMode.LINKED:
                            title = f"{frame.name}（只读）"
                        else:
                            embedded = session.document.embedded_document_for_frame(
                                surface.frame_id
                            )
                            title = f"{frame.name}{'*' if embedded.modified else ''}"
                    except ValueError:
                        continue
                index = self.surface_tabs.addTab(title)
                self.surface_tabs.setTabData(index, surface)
                if surface == session.active_surface:
                    self.surface_tabs.setCurrentIndex(index)
            if self.surface_tabs.count():
                self.surface_tabs.setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        finally:
            self.surface_tabs.blockSignals(False)

    def _workspace_tab_changed(self, index: int) -> None:
        if index >= 0 and hasattr(self.controller, "set_active_session"):
            self._capture_canvas_state()
            self.controller.set_active_session(index)
            self._restore_canvas_state()

    def _workspace_tab_close_requested(self, index: int) -> None:
        if hasattr(self.controller, "close_session"):
            self.controller.close_session(index, self)

    def _surface_tab_changed(self, index: int) -> None:
        if index < 0 or not hasattr(self.controller, "set_active_surface"):
            return
        surface = self.surface_tabs.tabData(index)
        if isinstance(surface, EditingSurfaceRef):
            self._capture_canvas_state()
            self.controller.set_active_surface(surface)
            self._restore_canvas_state()

    def _surface_tab_close_requested(self, index: int) -> None:
        surface = self.surface_tabs.tabData(index)
        if (
            isinstance(surface, EditingSurfaceRef)
            and surface.frame_id is not None
            and hasattr(self.controller, "close_frame_surface")
        ):
            self.controller.close_frame_surface(surface.frame_id)

    def _capture_canvas_state(self) -> None:
        session = getattr(self.controller, "active_session", None)
        if session is None or self.controller.document is None:
            return
        center = self.canvas_view.mapToScene(self.canvas_view.viewport().rect().center())
        tool_name = next(
            (
                name
                for name, action in self.tool_actions.items()
                if action.isChecked()
            ),
            "brush",
        )
        session.canvas_states[session.active_surface] = CanvasViewState(
            zoom=self.canvas_view.zoom,
            center_x=center.x(),
            center_y=center.y(),
            tool_name=tool_name,
        )

    def _restore_canvas_state(self) -> None:
        session = getattr(self.controller, "active_session", None)
        if session is None:
            return
        state = session.canvas_states.get(session.active_surface)
        if state is None:
            return
        self.canvas_view.set_zoom(state.zoom)
        self.canvas_view.centerOn(state.center_x, state.center_y)
        action = self.tool_actions.get(state.tool_name)
        if action is not None and action.isEnabled():
            action.setChecked(True)
            self._activate_tool(state.tool_name)

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
        scroller = QScrollArea(card)
        scroller.setObjectName(f"{object_name}ScrollArea")
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroller.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroller.setMinimumSize(0, 0)
        widget.setMinimumHeight(0)
        scroller.setWidget(widget)
        card_layout.addWidget(scroller)
        sidebar_layout.addWidget(card)

        dock.setWidget(sidebar)
        dock.setMinimumHeight(48)
        return dock

    def _request_new_document(self) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        self.controller.request_new_document(self)

    def _request_close_document(self) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        self.controller.close_document(self)

    def _request_open_document(self) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        self.controller.request_open_document(self)

    def _request_import_document(self) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        self.controller.request_import_document(self)

    def _request_append_document(self) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        self.controller.request_append_document(self)

    def _open_recent_path(self, path: Path) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        self.controller.open_recent_path(self, path)

    def _rebuild_recent_files_menu(self, *_args: object) -> None:
        menu: QMenu = self.recent_files_menu
        menu.clear()
        recent = self.controller.recent_files
        if not recent:
            empty_action = menu.addAction("（无最近文件）")
            empty_action.setEnabled(False)
        else:
            for index, path in enumerate(recent, start=1):
                label = f"&{index}  {path.name}"
                action = menu.addAction(label)
                action.setToolTip(str(path))
                action.setStatusTip(str(path))
                action.triggered.connect(
                    lambda _checked=False, selected=path: self._open_recent_path(selected)
                )
        menu.addSeparator()
        clear_action = menu.addAction("清空最近文件")
        clear_action.setEnabled(bool(recent))
        clear_action.triggered.connect(self.controller.clear_recent_files)

    def _request_save_document(self) -> None:
        self.controller.request_save(self)

    def _request_save_as_document(self) -> None:
        self.controller.request_save_as(self)

    def _request_export_image(self) -> None:
        self.controller.request_export_image(self)

    def _show_canvas_size_dialog(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        snapshot = DocumentSnapshot.capture(document)
        dialog = CanvasSizeDialog(document.size, self)

        def preview(size: object, anchor: object) -> None:
            if not isinstance(size, Size):
                return
            from easypicture.imaging import CanvasAnchor

            if not isinstance(anchor, CanvasAnchor):
                return
            offset = canvas_anchor_offset(snapshot.size, size, anchor)
            self.canvas_view.set_geometry_preview(
                size,
                AffineTransform.translation(float(offset.x), float(offset.y)),
            )

        dialog.preview_changed.connect(preview)
        preview(dialog.size_value, dialog.anchor)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.canvas_view.clear_geometry_preview()
        if not accepted:
            return
        request = CanvasResizeRequest(snapshot, dialog.size_value, dialog.anchor)
        self._start_geometry_job(
            lambda token, progress: calculate_canvas_resize(
                request,
                token,
                progress,
            ),
            "调整画布大小",
        )

    def _show_image_scale_dialog(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        snapshot = DocumentSnapshot.capture(document)
        interpolation = self.settings.transform_interpolation()
        dialog = ScaleGeometryDialog("缩放图像", document.size, interpolation, self)

        def preview(size: object, mode: object) -> None:
            from easypicture.imaging import TransformInterpolation

            if not isinstance(size, Size) or not isinstance(
                mode,
                TransformInterpolation,
            ):
                return
            self.canvas_view.set_geometry_preview(
                size,
                AffineTransform.scale(
                    size.width / snapshot.size.width,
                    size.height / snapshot.size.height,
                ),
                interpolation=mode,
            )

        dialog.preview_changed.connect(preview)
        preview(dialog.size_value, dialog.interpolation)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.canvas_view.clear_geometry_preview()
        if not accepted:
            return
        request = ImageScaleRequest(
            snapshot,
            dialog.size_value,
            dialog.interpolation,
        )
        self._start_geometry_job(
            lambda token, progress: calculate_image_scale(
                request,
                token,
                progress,
            ),
            "缩放图像",
        )

    def _show_layer_scale_dialog(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        layer = document.active_layer
        if layer.locked or layer.alpha_locked:
            self._show_operation_status("锁定或透明像素锁定的图层不能缩放")
            return
        snapshot = DocumentSnapshot.capture(document)
        interpolation = self.settings.transform_interpolation()
        dialog = ScaleGeometryDialog("缩放图层", layer.size, interpolation, self)

        def preview(size: object, mode: object) -> None:
            from easypicture.imaging import TransformInterpolation

            if not isinstance(size, Size) or not isinstance(
                mode,
                TransformInterpolation,
            ):
                return
            scale_x = size.width / layer.size.width
            scale_y = size.height / layer.size.height
            matrix = AffineTransform(
                a=scale_x,
                d=scale_y,
                tx=layer.origin.x * (1.0 - scale_x),
                ty=layer.origin.y * (1.0 - scale_y),
            )
            self.canvas_view.set_geometry_preview(
                document.size,
                matrix,
                layer_id=layer.id,
                interpolation=mode,
            )

        dialog.preview_changed.connect(preview)
        preview(dialog.size_value, dialog.interpolation)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        self.canvas_view.clear_geometry_preview()
        if not accepted:
            return
        request = LayerScaleRequest(
            snapshot,
            layer.id,
            dialog.size_value,
            dialog.interpolation,
        )
        self._start_geometry_job(
            lambda token, progress: calculate_layer_scale(
                request,
                token,
                progress,
            ),
            "缩放图层",
            clear_sprite_frames_on_success=False,
        )

    def _crop_to_selection(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        snapshot = DocumentSnapshot.capture(document)
        rect = selection_bounds(snapshot)
        if rect is None:
            self._show_operation_status("当前没有活动选区")
            return
        self._commit_crop_rect(rect)

    def _trim_transparent(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        request = TrimTransparentRequest(DocumentSnapshot.capture(document))
        self._start_geometry_job(
            lambda token, progress: calculate_trim_transparent(
                request,
                token,
                progress,
            ),
            "裁切透明像素",
        )

    def _clear_selected_pixels(self) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        transform = self._tools.get("transform")
        if isinstance(transform, TransformTool) and transform.has_active_session:
            self._show_operation_status("请先应用或取消自由变换")
            return
        if not document.selection.is_active:
            self._show_operation_status("当前没有活动选区")
            return
        layer = document.active_layer
        if layer.locked or layer.alpha_locked:
            self._show_operation_status("锁定或透明像素锁定的图层不能清除")
            return
        if not self.controller.commands.clear_selected_pixels(layer.id):
            self._show_operation_status("选区内没有可清除的像素")

    def _start_geometry_job(
        self,
        operation: JobOperation,
        label: str,
        *,
        clear_sprite_frames_on_success: bool = True,
    ) -> None:
        if self._geometry_job is not None:
            return
        document = self.controller.document
        clear_frames = bool(
            clear_sprite_frames_on_success
            and document is not None
            and document.kind is DocumentKind.PROJECT
            and document.sprite_workspace.frames
        )
        if clear_frames:
            answer = QMessageBox.question(
                self,
                label,
                "此操作会清空现有精灵帧定义；撤销几何操作可同时恢复。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        self._geometry_clear_sprite_frames = clear_frames
        handle = self.controller.jobs.submit(operation)
        self._geometry_job = handle
        dialog = QProgressDialog(f"正在{label}…", "取消", 0, 0, self)
        dialog.setWindowTitle(label)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setMinimumDuration(150)
        dialog.setAutoClose(False)
        dialog.canceled.connect(handle.cancel)
        handle.progress.connect(
            lambda value, maximum, message: self._update_selection_progress(
                dialog,
                value,
                maximum,
                message,
            )
        )
        self._geometry_progress = dialog
        self._set_editing_busy(True)
        handle.finished.connect(lambda outcome: self._on_geometry_finished(outcome, label))

    def _on_geometry_finished(self, outcome: JobOutcome, label: str) -> None:
        self._close_geometry_progress()
        self._geometry_job = None
        self._set_editing_busy(False)
        clear_sprite_frames = self._geometry_clear_sprite_frames
        self._geometry_clear_sprite_frames = False
        if outcome.status is JobStatus.CANCELLED:
            self._show_operation_status(f"已取消{label}")
            return
        if outcome.status is JobStatus.FAILED:
            self._show_operation_status(f"{label}失败：{outcome.error}")
            return
        if not isinstance(outcome.value, DocumentGeometryResult):
            self._show_operation_status(f"{label}失败：无效的任务结果")
            return
        if not self.controller.commands.apply_document_geometry(
            outcome.value,
            label,
            clear_sprite_frames=clear_sprite_frames,
        ):
            self._show_operation_status(f"{label}未产生变化或结果已过期")

    def _close_geometry_progress(self) -> None:
        if self._geometry_progress is not None:
            self._geometry_progress.close()
            self._geometry_progress.deleteLater()
            self._geometry_progress = None

    def _on_document_changed(self, document: Document | None) -> None:
        project_document = self._project_document()
        self._selected_vector_object = False
        self._sprite_editing_frame_id = None
        if hasattr(self, "color_adjustment_controller") and self.color_adjustment_controller.active:
            self.color_adjustment_controller.cancel()
        if self._selection_job is not None:
            self._selection_job.cancel()
            self._selection_job = None
            self._close_selection_progress()
            self._editing_busy = False
        if self._geometry_job is not None:
            self._geometry_job.cancel()
            self._geometry_job = None
            self._close_geometry_progress()
            self._editing_busy = False
        self.canvas_view.clear_geometry_preview()
        self.canvas_view.set_document(document)
        self.tool_controller.reset_active_tool()
        self.layer_panel.set_document(document)
        self.sprite_properties.set_document(project_document)
        self.sprite_properties.set_region(None)
        self.sprite_properties.set_frame(None)
        self.sprite_frame_panel.set_document(project_document)
        active_session = getattr(self.controller, "active_session", None)
        if active_session is not None and active_session.active_frame_group_id is not None:
            self.sprite_frame_panel.set_active_group(active_session.active_frame_group_id)
        self._group_expansion_state = {}
        self._pending_group_expansion = None
        self._restore_group_expansion()
        self._schedule_group_expansion_restore()
        self._warn_missing_text_fonts(document)
        has_document = document is not None
        self.central_stack.setCurrentWidget(self.canvas_view if has_document else self.empty_state)
        self.close_action.setEnabled(has_document)
        self.save_action.setEnabled(has_document)
        self.save_as_action.setEnabled(has_document)
        self.export_action.setEnabled(has_document)
        self.export_sprite_frame_action.setEnabled(False)
        self.export_sprite_atlas_action.setEnabled(
            bool(project_document and project_document.sprite_workspace.frames)
        )
        linked_read_only = self._active_surface_is_linked()
        self.append_action.setEnabled(
            has_document and not linked_read_only and not self._editing_busy
        )
        self.transform_action.setEnabled(
            has_document and not linked_read_only and not self._editing_busy
        )
        source_surface = document is not None and document.kind is DocumentKind.PROJECT
        for action in (
            self.canvas_size_action,
            self.image_scale_action,
            self.layer_scale_action,
            self.trim_transparent_action,
        ):
            action.setEnabled(source_surface and not self._editing_busy)
        for action in (
            self.zoom_in_action,
            self.zoom_out_action,
            self.fit_action,
            self.actual_size_action,
        ):
            action.setEnabled(has_document)
        self._refresh_tool_actions(has_document)
        self.tool_options_panel.setEnabled(
            has_document and not linked_read_only and not self._editing_busy
        )
        self.layer_panel.setEnabled(not linked_read_only and not self._editing_busy)
        for action in self.color_adjustment_actions.values():
            action.setEnabled(
                has_document and not linked_read_only and not self._editing_busy
            )
        if document is None:
            self.document_size_label.setText("无文档")
            self.sprite_count_label.setText("帧 0")
        else:
            self.document_size_label.setText(f"{document.width} × {document.height} px")
            self.sprite_count_label.setText(
                f"帧 {len(project_document.sprite_workspace.frames) if project_document else 0}"
            )
        if "sprite_slice" in self.tool_actions:
            self.tool_actions["sprite_slice"].setEnabled(
                source_surface and not self._editing_busy
            )
        self._refresh_workspace_tabs()
        self._update_undo_actions()
        self._update_selection_actions()
        self._update_window_title()

    def _activate_tool(self, tool_name: str) -> None:
        if hasattr(self, "shortcut_hint_label"):
            self._update_shortcut_hint(tool_name)
        if hasattr(self, "color_adjustment_controller") and self.color_adjustment_controller.active:
            self.color_adjustment_controller.cancel()
        if tool_name == "crop":
            document = self.controller.document
            if document is not None:
                self.tool_options_panel.crop.set_original_size(
                    document.width,
                    document.height,
                )
        actual_tool_name = (
            "text_transform"
            if tool_name == "transform" and self._selected_vector_object
            else tool_name
        )
        tool = self._tools[actual_tool_name]
        if tool is self.tool_controller.active_tool and isinstance(tool, TransformTool):
            tool.restart_session()
        elif tool is self.tool_controller.active_tool and isinstance(tool, TextTransformTool):
            self.tool_controller.reset_active_tool()
        else:
            self.tool_controller.set_tool(tool)
        self.tool_options_panel.show_tool(tool_name)
        if tool_name == "transform" and hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentWidget(
                self.text_properties if self._selected_vector_object else self.transform_properties
            )
            self.properties_dock.show()
            self.properties_dock.raise_()
        elif tool_name == "shape" and hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentWidget(self.shape_properties)
            self.properties_dock.show()
            self.properties_dock.raise_()
        elif tool_name == "sprite_slice" and hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentWidget(self.sprite_properties)
            self.properties_dock.show()
            self.properties_dock.raise_()
            if not self.sprite_frames_dock.isVisible():
                self.sprite_frames_dock.show()
            self.sprite_frames_dock.raise_()
            self._resize_sprite_frame_dock()
            QTimer.singleShot(0, self._resize_sprite_frame_dock)
        elif hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentIndex(0)

    def _activate_transform_command(self) -> None:
        if self.controller.document is None or self._editing_busy:
            return
        self.tool_actions["transform"].setChecked(True)
        self._activate_tool("transform")

    def _set_sprite_region(self, region: Rect | None) -> None:
        self.sprite_properties.set_region(region)

    def _sprite_region_requested(self, region: object) -> None:
        tool = self._tools.get("sprite_slice")
        if isinstance(tool, SpriteSliceTool) and isinstance(region, Rect):
            tool.set_region(region)

    def _confirm_sprite_region(self, region: Rect) -> None:
        document = self._project_document()
        if document is None:
            return
        frame_id = self._sprite_editing_frame_id
        if frame_id is None:
            if not self.controller.commands.create_sprite_frame(region):
                self._show_operation_status("无法创建精灵帧")
                return
            self._sprite_editing_frame_id = None
            self.sprite_properties.set_frame(None)
            self._show_operation_status("已添加精灵帧")
            return
        self.controller.commands.update_sprite_frame(frame_id, region=region)

    def _cancel_sprite_slice(self) -> None:
        self.tool_actions["brush"].setChecked(True)
        self._activate_tool("brush")

    def _select_sprite_frame(self, frame_id: object) -> None:
        if frame_id is not None and not isinstance(frame_id, UUID):
            return
        self._sprite_editing_frame_id = frame_id
        document = self.controller.document
        frame = None
        if document is not None and frame_id is not None:
            try:
                frame = document.sprite_workspace.frame_by_id(frame_id)
            except ValueError:
                frame = None
        self.sprite_properties.set_frame(frame)
        self.export_sprite_frame_action.setEnabled(frame is not None)
        if frame is None:
            return
        if self.tool_controller.active_tool is not self._tools["sprite_slice"]:
            self.tool_actions["sprite_slice"].setChecked(True)
            self._activate_tool("sprite_slice")
        tool = self._tools.get("sprite_slice")
        if isinstance(tool, SpriteSliceTool):
            tool.set_region(frame.region)

    def _new_sprite_frame(self) -> None:
        self._sprite_editing_frame_id = None
        self.sprite_frame_panel.select_frame(None)
        self.sprite_properties.set_frame(None)
        if self.tool_controller.active_tool is not self._tools["sprite_slice"]:
            self.tool_actions["sprite_slice"].setChecked(True)
            self._activate_tool("sprite_slice")

    def _delete_sprite_frame(self, frame_id: object) -> None:
        frame_ids = (
            frame_id
            if isinstance(frame_id, tuple)
            and all(isinstance(item, UUID) for item in frame_id)
            else (frame_id,) if isinstance(frame_id, UUID) else ()
        )
        if not frame_ids:
            return
        if self.controller.commands.delete_sprite_frames(frame_ids):
            self._sprite_editing_frame_id = None
            self.sprite_properties.set_frame(None)

    def _rename_sprite_frame(self, frame_id: object, name: str) -> None:
        if isinstance(frame_id, UUID):
            self.controller.commands.update_sprite_frame(frame_id, name=name)

    def _move_sprite_frame(self, frame_id: object, target_index: int) -> None:
        if isinstance(frame_id, UUID):
            group_id = self.sprite_frame_panel.active_group_id
            if isinstance(group_id, UUID) or group_id is None:
                self.controller.commands.move_sprite_frame_to_group(
                    frame_id,
                    group_id,
                    target_index,
                )
            else:
                self.controller.commands.move_sprite_frame(frame_id, target_index)

    def _open_sprite_frame_surface(self, frame_id: object) -> None:
        if not isinstance(frame_id, UUID) or not hasattr(self.controller, "open_frame_surface"):
            return
        project = self._project_document()
        if project is None:
            return
        try:
            project.sprite_workspace.frame_by_id(frame_id)
        except ValueError:
            return
        self.controller.open_frame_surface(frame_id)

    def _detach_sprite_frame(self, frame_id: object) -> None:
        if isinstance(frame_id, UUID) and self.controller.commands.detach_sprite_frame(frame_id):
            if hasattr(self.controller, "open_frame_surface"):
                self.controller.open_frame_surface(frame_id)

    def _reset_sprite_frame(self, frame_id: object) -> None:
        if not isinstance(frame_id, UUID):
            return
        if hasattr(self.controller, "set_active_surface"):
            self.controller.set_active_surface(EditingSurfaceRef("source"))
        if self.controller.commands.reset_sprite_frame_to_source(frame_id):
            if hasattr(self.controller, "close_frame_surface"):
                self.controller.close_frame_surface(frame_id)

    def _duplicate_sprite_frame(self, frame_id: object) -> None:
        if isinstance(frame_id, UUID):
            self.controller.commands.duplicate_sprite_frame(frame_id)

    def _sprite_group_selected(self, group_id: object) -> None:
        session = getattr(self.controller, "active_session", None)
        if session is not None:
            session.active_frame_group_id = group_id if isinstance(group_id, UUID) else None

    def _create_sprite_group(self, name: str) -> None:
        self.controller.commands.create_sprite_frame_group(name)

    def _rename_sprite_group(self, group_id: object, name: str) -> None:
        if isinstance(group_id, UUID):
            self.controller.commands.rename_sprite_frame_group(group_id, name)

    def _move_sprite_group(self, group_id: object, target_index: int) -> None:
        if isinstance(group_id, UUID):
            self.controller.commands.move_sprite_frame_group(group_id, target_index)

    def _move_sprite_frame_to_group(
        self,
        frame_id: object,
        group_id: object,
        target_index: int,
    ) -> None:
        frame_ids = (
            frame_id
            if isinstance(frame_id, tuple)
            and all(isinstance(item, UUID) for item in frame_id)
            else (frame_id,) if isinstance(frame_id, UUID) else ()
        )
        if frame_ids and (group_id is None or isinstance(group_id, UUID)):
            self.controller.commands.move_sprite_frames_to_group(
                frame_ids,
                group_id,
                target_index,
            )

    def _delete_sprite_group(self, group_id: object) -> None:
        if not isinstance(group_id, UUID):
            return
        project = self._project_document()
        if project is None:
            return
        try:
            group = project.sprite_workspace.group_by_id(group_id)
        except ValueError:
            return
        if not group.frame_ids:
            self.controller.commands.delete_sprite_frame_group(
                group_id,
                GroupDeleteMode.MOVE_TO_UNGROUPED,
            )
            return
        box = QMessageBox(self)
        box.setWindowTitle("删除帧分组")
        box.setText("此分组包含精灵帧。")
        move_button = box.addButton("移到未分组", QMessageBox.ButtonRole.AcceptRole)
        delete_button = box.addButton("同时删除帧", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is move_button:
            mode = GroupDeleteMode.MOVE_TO_UNGROUPED
        elif box.clickedButton() is delete_button:
            mode = GroupDeleteMode.DELETE_FRAMES
        else:
            return
        self.controller.commands.delete_sprite_frame_group(group_id, mode)

    def _reorder_sprite_frames(self, frame_ids: object, label: str) -> None:
        if isinstance(frame_ids, tuple) and all(
            isinstance(frame_id, UUID) for frame_id in frame_ids
        ):
            group_id = self.sprite_frame_panel.active_group_id
            if isinstance(group_id, UUID):
                self.controller.commands.reorder_sprite_frame_group(
                    group_id,
                    frame_ids,
                    label=label,
                )
            elif group_id is None:
                self.controller.commands.reorder_ungrouped_sprite_frames(
                    frame_ids,
                    label=label,
                )
            else:
                self.controller.commands.reorder_sprite_frames(frame_ids, label=label)

    def _resize_sprite_frame_dock(self) -> None:
        height = self.sprite_frame_panel.preferred_dock_height()
        self.sprite_frames_dock.setMinimumHeight(height)
        self.sprite_frames_dock.setMaximumHeight(16_777_215)
        if self.sprite_frames_dock.isVisible() and self.sprite_frames_dock.height() < height:
            self.resizeDocks(
                [self.sprite_frames_dock],
                [height],
                Qt.Orientation.Vertical,
            )

    def _update_sprite_frame_details(
        self,
        frame_id: object,
        name: object,
        pivot: object,
    ) -> None:
        if isinstance(frame_id, UUID) and isinstance(name, str) and isinstance(
            pivot,
            PixelPoint,
        ):
            self.controller.commands.update_sprite_frame(
                frame_id,
                name=name,
                pivot=pivot,
            )

    def _set_sprite_cell_size(self, size: object) -> None:
        document = self._project_document()
        if document is None or not isinstance(size, Size):
            return
        clear_frames = False
        if document.sprite_workspace.frames:
            answer = QMessageBox.question(
                self,
                "更改单格尺寸",
                "更改单格尺寸会清空现有精灵帧定义。是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                self.sprite_properties.set_document(document)
                return
            clear_frames = True
        if self.controller.commands.set_sprite_cell_size(size, clear_frames=clear_frames):
            self._sprite_editing_frame_id = None
            tool = self._tools.get("sprite_slice")
            if isinstance(tool, SpriteSliceTool):
                tool.reset_for_cell_size()

    def _export_sprite_frame(self, frame_id: object) -> None:
        if isinstance(frame_id, UUID):
            self.controller.request_export_sprite_frame(self, frame_id)

    def _export_selected_sprite_frame(self) -> None:
        frame_id = self.sprite_frame_panel.selected_frame_id
        if frame_id is not None:
            self._export_sprite_frame(frame_id)

    def _export_sprite_atlas(self) -> None:
        project = self._project_document()
        if project is None or not project.sprite_workspace.frames:
            return
        suggested = Path(f"{Path(project.display_name).stem or 'sprites'}_atlas.png")
        dialog = AtlasExportDialog(DocumentSnapshot.capture(project), suggested, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        spec = dialog.spec
        self.controller.export_sprite_atlas_to(
            self,
            spec.path,
            included_group_ids=spec.included_group_ids,
            include_ungrouped=spec.include_ungrouped,
            horizontal_spacing=spec.horizontal_spacing,
            vertical_spacing=spec.vertical_spacing,
            margin=spec.margin,
        )

    def _set_pixel_exact_coordinates(self, enabled: bool) -> None:
        self.settings.set_pixel_exact_coordinates(enabled)
        self.settings.sync()

    def _set_pixel_grid_visible(self, visible: bool) -> None:
        self.settings.set_pixel_grid_visible(visible)
        self.settings.sync()
        self.canvas_view.set_sprite_grid_visibility(
            pixel_grid=visible,
            cell_grid=self.sprite_grid_action.isChecked(),
        )

    def _set_sprite_grid_visible(self, visible: bool) -> None:
        self.settings.set_sprite_grid_visible(visible)
        self.settings.sync()
        self.canvas_view.set_sprite_grid_visibility(
            pixel_grid=self.pixel_grid_action.isChecked(),
            cell_grid=visible,
        )

    def _set_sprite_snap_enabled(self, enabled: bool) -> None:
        self.settings.set_sprite_grid_snap_enabled(enabled)
        self.settings.sync()

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

    def _shape_kind_changed(self, kind: object) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.kind_changed()
        if isinstance(kind, ShapeKind):
            self.settings.set_shape_options(
                kind,
                self.tool_options_panel.shape.settings(),
                self.tool_options_panel.shape.output_target(),
            )
            self.settings.sync()

    def _shape_settings_changed(self, settings: object) -> None:
        if not isinstance(settings, ShapeSettings):
            return
        self.settings.set_shape_options(
            self.tool_options_panel.shape.kind(),
            settings,
            self.tool_options_panel.shape.output_target(),
        )
        self.settings.sync()
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.settings_changed()

    def _shape_output_target_changed(self, target: object) -> None:
        if not isinstance(target, ShapeOutputTarget):
            return
        self.settings.set_shape_options(
            self.tool_options_panel.shape.kind(),
            self.tool_options_panel.shape.settings(),
            target,
        )
        self.settings.sync()
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.cancel()

    def _set_shape_geometry(self, geometry: ShapeGeometry | None) -> None:
        if hasattr(self, "shape_properties"):
            self.shape_properties.set_geometry(geometry)

    def _shape_geometry_changed(self, geometry: object) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool) and isinstance(geometry, ShapeGeometry):
            tool.set_geometry(geometry)

    def _shape_edit_started(self, _label: str) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.begin_property_edit()

    def _shape_edit_finished(self) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.finish_property_edit()

    def _shape_edit_canceled(self) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.cancel_property_edit()

    def _shape_commit(self) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.commit()

    def _shape_cancel(self) -> None:
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.cancel()

    def _svg_preview_changed(self, preview: object) -> None:
        document = self._project_document()
        if document is None or not isinstance(preview, SvgElementObject):
            return
        try:
            group, current = document.vector_object_by_id(preview.id)
        except ValueError:
            self.canvas_view.clear_vector_preview()
            return
        objects = list(group.children)
        objects[group.children.index(current)] = preview
        self.canvas_view.replace_vector_preview(group.id, tuple(objects))
        tool = self._tools.get("shape")
        if isinstance(tool, ShapeTool):
            tool.set_svg_preview_object(preview)

    def _start_color_adjustment(self, kind: ColorAdjustmentKind) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        previous_tool = self.tool_controller.active_tool
        self._adjustment_previous_tool = previous_tool
        self.tool_controller.set_tool(NullTool())
        parameters = self.color_adjustment_panel.activate(kind)
        if not self.color_adjustment_controller.start(parameters):
            self.tool_controller.set_tool(previous_tool)
            self._adjustment_previous_tool = None

    def _color_adjustment_parameters_changed(self, parameters: object) -> None:
        if not isinstance(
            parameters,
            (
                InvertAdjustment,
                ThresholdAdjustment,
                BrightnessContrastAdjustment,
                GammaAdjustment,
                CurvesAdjustment,
                HslAdjustment,
                GradientMapAdjustment,
                ColorReplaceAdjustment,
                PaletteMapAdjustment,
            ),
        ):
            return
        if self.color_adjustment_controller.update_parameters(parameters):
            self.color_adjustment_panel.set_running(True)
        elif self.color_adjustment_controller.parameters is not None:
            self.color_adjustment_panel.set_parameters(self.color_adjustment_controller.parameters)

    def _apply_color_adjustment(self) -> None:
        self.color_adjustment_panel.set_running(True)
        self.color_adjustment_controller.apply()

    def _apply_color_adjustment_if_active(self) -> None:
        if self.color_adjustment_controller.active:
            self._apply_color_adjustment()

    def _on_color_adjustment_active(self, active: bool) -> None:
        if active:
            self._set_editing_busy(True)
            self.properties_stack.setCurrentWidget(self.color_adjustment_panel)
            self.properties_dock.show()
            self.properties_dock.raise_()
            return
        self.color_adjustment_panel.set_running(False)
        self.color_adjustment_panel.set_progress(0, 0, "")
        self._set_editing_busy(False)
        previous = self._adjustment_previous_tool
        self._adjustment_previous_tool = None
        if previous is not None and self.controller.document is not None:
            self.tool_controller.set_tool(previous)
            for name, tool in self._tools.items():
                if tool is previous:
                    self.tool_actions[name].setChecked(True)
                    self.tool_options_panel.show_tool(name)
                    self._show_properties_for_tool(name)
                    break
        elif hasattr(self, "properties_stack"):
            self.properties_stack.setCurrentIndex(0)

    def _show_properties_for_tool(self, tool_name: str) -> None:
        if tool_name == "transform":
            self.properties_stack.setCurrentWidget(
                self.text_properties if self._selected_vector_object else self.transform_properties
            )
        elif tool_name == "shape":
            self.properties_stack.setCurrentWidget(self.shape_properties)
        elif tool_name == "text":
            self.properties_stack.setCurrentWidget(self.text_properties)
        else:
            self.properties_stack.setCurrentIndex(0)

    def _on_layer_node_selected(self, node: object) -> None:
        from easypicture.domain import TextObject

        self._selected_vector_object = isinstance(node, (TextObject, SvgElementObject))
        if isinstance(node, TextObject):
            self.text_properties.set_object(node)
            self.properties_stack.setCurrentWidget(self.text_properties)
            self.properties_dock.show()
            self.properties_dock.raise_()
            if not self._editing_busy:
                self.tool_actions["transform"].setChecked(True)
                self._activate_tool("transform")
        elif isinstance(node, SvgElementObject):
            self.text_properties.set_object(None)
            self.svg_properties.set_object(node)
            if parse_editable_svg(node.svg) is not None and not self._editing_busy:
                self.tool_options_panel.shape.output_combo.setCurrentIndex(
                    self.tool_options_panel.shape.output_combo.findData(
                        ShapeOutputTarget.VECTOR.value
                    )
                )
                self.tool_actions["shape"].setChecked(True)
                self._activate_tool("shape")
                tool = self._tools.get("shape")
                if isinstance(tool, ShapeTool):
                    tool.edit_svg_object(node)
            self.properties_stack.setCurrentWidget(self.svg_properties)
            self.properties_dock.show()
            self.properties_dock.raise_()
        elif isinstance(self.tool_controller.active_tool, TextTransformTool):
            self.tool_actions["brush"].setChecked(True)
            self._activate_tool("brush")
        self._refresh_tool_actions(self.controller.document is not None)

    def _warn_missing_text_fonts(self, document: Document | None) -> None:
        if document is None:
            return
        for group in document.vector_groups:
            for text_object in group.children:
                if not isinstance(text_object, TextObject):
                    continue
                family = text_object.font_family
                if family in self._warned_text_fonts or QFontDatabase.hasFamily(family):
                    continue
                self._warned_text_fonts.add(family)
                self._show_operation_status(
                    f"字体“{family}”不可用，文字将使用系统回退字体",
                    8000,
                )

    def _set_group_expanded(self, index: QModelIndex, expanded: bool) -> None:
        if not index.isValid():
            return
        node = self.layer_panel.tree_model.node_for_index(index)
        from easypicture.domain import RasterGroupLayer, VectorGroupLayer

        if isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
            self._group_expansion_state[node.id] = expanded
            if (
                self._pending_group_expansion is not None
                and node.id in self._pending_group_expansion
            ):
                self._pending_group_expansion[node.id] = expanded
            self.settings.set_group_expanded(node.id, expanded)
            self.settings.sync()

    def _capture_group_expansion(self) -> dict[UUID, bool]:
        """Capture expansion by UUID, independent of a pending model reset.

        Document mutations happen before their domain event is forwarded.  At
        that point the document already contains the new group while the Qt
        model still contains the old rows, so querying ``isExpanded`` by row
        can associate the wrong state with the wrong group.  The UUID map is
        updated only by expansion signals and is therefore stable across that
        gap.
        """
        from easypicture.domain import RasterGroupLayer, VectorGroupLayer

        document = self.controller.document
        if document is None:
            return dict(self._group_expansion_state)
        state: dict[UUID, bool] = {}
        for node in document.nodes:
            if isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
                state[node.id] = self._group_expansion_state.get(
                    node.id,
                    self.settings.group_expanded(node.id),
                )
        return state

    def _restore_group_expansion(
        self,
        preserved: dict[UUID, bool] | None = None,
    ) -> None:
        if not hasattr(self, "layer_panel"):
            return
        from easypicture.domain import RasterGroupLayer, VectorGroupLayer

        tree = self.layer_panel.tree_view
        model = self.layer_panel.tree_model
        current_ids: set[UUID] = set()
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            node = model.node_for_index(index)
            if isinstance(node, (RasterGroupLayer, VectorGroupLayer)):
                current_ids.add(node.id)
                expanded = (
                    preserved[node.id]
                    if preserved is not None and node.id in preserved
                    else self.settings.group_expanded(node.id)
                )
                self._group_expansion_state[node.id] = expanded
                tree.setExpanded(index, expanded)
        self._group_expansion_state = {
            group_id: expanded
            for group_id, expanded in self._group_expansion_state.items()
            if group_id in current_ids
        }

    def _schedule_group_expansion_restore(
        self,
        preserved: dict[UUID, bool] | None = None,
    ) -> None:
        """Restore expansion after Qt finishes processing a model reset.

        Coalescing a structural event burst and applying it on the next event-loop
        turn keeps the user's state authoritative while Qt finishes reset handling.
        """
        if preserved is not None and self._pending_group_expansion is None:
            self._pending_group_expansion = dict(preserved)
        if self._group_expansion_restore_scheduled:
            return
        self._group_expansion_restore_scheduled = True
        QTimer.singleShot(0, self._restore_pending_group_expansion)

    def _restore_pending_group_expansion(self) -> None:
        preserved = self._pending_group_expansion
        self._pending_group_expansion = None
        self._group_expansion_restore_scheduled = False
        self._restore_group_expansion(preserved)

    def _refresh_tool_actions(self, has_document: bool) -> None:
        raster_tools = {
            "brush",
            "eraser",
            "eyedropper",
            "bucket",
            "rectangle_selection",
            "magic_wand",
            "crop",
            "shape",
        }
        for name, action in self.tool_actions.items():
            enabled = has_document and not self._editing_busy
            if self._active_surface_is_linked() and name != "eyedropper":
                enabled = False
            if self._selected_vector_object and name in raster_tools:
                enabled = False
            if (
                self.controller.document is not None
                and self.controller.document.kind is DocumentKind.SPRITE_FRAME
                and name in {"crop", "sprite_slice"}
            ):
                enabled = False
            action.setEnabled(enabled)

    def _active_surface_is_linked(self) -> bool:
        session = getattr(self.controller, "active_session", None)
        return bool(session is not None and session.active_surface_is_linked)

    def _cancel_color_adjustment_for_lifecycle(self) -> None:
        if hasattr(self, "color_adjustment_controller") and self.color_adjustment_controller.active:
            self.color_adjustment_controller.cancel()

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

    def _set_crop_rect(self, rect: GeometryRect | None) -> None:
        self.tool_options_panel.crop.set_rect(rect)

    def _crop_rect_changed(self, value: object) -> None:
        tool = self._tools.get("crop")
        if isinstance(tool, CropTool) and isinstance(value, GeometryRect):
            tool.set_rect(value)

    def _crop_options_changed(self) -> None:
        tool = self._tools.get("crop")
        if isinstance(tool, CropTool):
            tool.refresh_options()

    def _crop_commit(self) -> None:
        tool = self._tools.get("crop")
        if isinstance(tool, CropTool):
            tool.commit()

    def _crop_cancel(self) -> None:
        if self.controller.document is None:
            return
        self.tool_actions["brush"].setChecked(True)
        self._activate_tool("brush")

    def _commit_crop_rect(self, rect: GeometryRect) -> None:
        document = self.controller.document
        if document is None or self._editing_busy:
            return
        request = CropDocumentRequest(DocumentSnapshot.capture(document), rect)
        self.tool_actions["brush"].setChecked(True)
        self._activate_tool("brush")
        self._start_geometry_job(
            lambda token, progress: calculate_crop_document(
                request,
                token,
                progress,
            ),
            "裁切画布",
        )

    def _show_preferences(self) -> None:
        dialog = PreferencesDialog(
            self.settings.selection_outline_color(),
            self.settings.selection_outline_width(),
            self.settings.render_backend(),
            self.canvas_view.render_coordinator.diagnostics,
            self,
            numeric_drag_sensitivity=self.settings.numeric_drag_sensitivity(),
            numeric_fine_drag_sensitivity=(self.settings.numeric_fine_drag_sensitivity()),
            pixel_exact_coordinates=self.settings.pixel_exact_coordinates(),
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
        self.settings.set_pixel_exact_coordinates(dialog.pixel_exact_coordinates)
        self.pixel_exact_action.setChecked(dialog.pixel_exact_coordinates)
        self.settings.sync()
        configure_numeric_drag_sensitivity(
            dialog.numeric_drag_sensitivity,
            dialog.numeric_fine_drag_sensitivity,
        )
        self.canvas_view.set_selection_outline_style(
            dialog.selection_color,
            dialog.selection_width,
        )

    def _show_about(self) -> None:
        dialog = AboutDialog(
            self.runtime_paths,
            self.canvas_view.render_coordinator.diagnostics,
            self,
        )
        dialog.exec()

    def _open_log_folder(self) -> None:
        self.runtime_paths.logs.mkdir(parents=True, exist_ok=True)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.runtime_paths.logs))):
            self.controller.notifications.warning(
                "open-log-folder-failed",
                "无法打开日志目录",
                details=str(self.runtime_paths.logs),
            )

    def _show_user_notice(self, notice: UserNotice) -> None:
        prefix = {
            NoticeSeverity.INFO: "",
            NoticeSeverity.WARNING: "警告：",
            NoticeSeverity.ERROR: "错误：",
        }[notice.severity]
        timeout = 5_000 if notice.severity is NoticeSeverity.INFO else 10_000
        self._show_operation_status(f"{prefix}{notice.message}", timeout)

    def _on_render_backend_fallback(self, reason: str) -> None:
        logging.getLogger("easypicture.render").warning(
            "OpenGL unavailable; using software backend: %s",
            reason,
        )
        self.controller.notifications.warning(
            "render-backend-fallback",
            "OpenGL 渲染不可用，已回退到软件后端",
            details=reason,
        )

    def _on_render_diagnostics(self, diagnostics: object) -> None:
        logging.getLogger("easypicture.render").info(
            "render diagnostics: %s",
            diagnostics,
        )

    def _set_editing_busy(self, busy: bool) -> None:
        self._editing_busy = busy
        has_document = self.controller.document is not None
        linked_read_only = self._active_surface_is_linked()
        self._refresh_tool_actions(has_document)
        self.tool_options_panel.setEnabled(has_document and not linked_read_only and not busy)
        self.layer_panel.setEnabled(not linked_read_only and not busy)
        for action in self.color_adjustment_actions.values():
            action.setEnabled(has_document and not linked_read_only and not busy)
        for action in (
            self.save_action,
            self.save_as_action,
            self.export_action,
            self.append_action,
        ):
            action.setEnabled(has_document and not linked_read_only and not busy)
        self.transform_action.setEnabled(has_document and not linked_read_only and not busy)
        source_surface = (
            self.controller.document is not None
            and self.controller.document.kind is DocumentKind.PROJECT
        )
        for action in (
            self.canvas_size_action,
            self.image_scale_action,
            self.layer_scale_action,
            self.trim_transparent_action,
        ):
            action.setEnabled(source_surface and not busy)
        self._update_selection_actions()
        self._update_undo_actions()

    def _on_document_event(self, event: DocumentEvent) -> None:
        tree_reset_event = isinstance(
            event,
            (GroupListChanged, GroupMembersChanged),
        )
        preserved_expansion = (
            self._capture_group_expansion()
            if tree_reset_event and self._pending_group_expansion is None
            else None
        )
        self.layer_panel.handle_event(event)
        if tree_reset_event:
            # Restore once in the same call stack so the reset cannot be
            # painted as a collapsed frame, then repeat on the next event
            # loop turn for any view-internal reset bookkeeping.
            restore_state = (
                self._pending_group_expansion
                if self._pending_group_expansion is not None
                else preserved_expansion
            )
            self._restore_group_expansion(restore_state)
            self._schedule_group_expansion_restore(preserved_expansion)
        document = self.controller.document
        project_document = self._project_document()
        if document is not None:
            self.document_size_label.setText(f"{document.width} × {document.height} px")
            self.sprite_count_label.setText(
                f"帧 {len(project_document.sprite_workspace.frames) if project_document else 0}"
            )
            if isinstance(event, SpriteWorkspaceChanged):
                selected = self._sprite_editing_frame_id
                self.sprite_properties.set_document(project_document)
                if project_document is not None:
                    self.sprite_frame_panel.handle_workspace_change(event)
                if selected is not None and project_document is not None:
                    try:
                        frame = project_document.sprite_workspace.frame_by_id(selected)
                    except ValueError:
                        self._sprite_editing_frame_id = None
                        self.sprite_properties.set_frame(None)
                    else:
                        self.sprite_properties.set_frame(frame)
            elif isinstance(event, SpriteFrameContentChanged):
                self.sprite_frame_panel.invalidate_thumbnails((event.frame_id,))
            elif document.kind is DocumentKind.SPRITE_FRAME:
                session = getattr(self.controller, "active_session", None)
                frame_id = (
                    session.active_surface.frame_id
                    if session is not None and session.active_surface.kind == "frame"
                    else None
                )
                if frame_id is not None:
                    self.sprite_frame_panel.invalidate_thumbnails((frame_id,))
            else:
                self.sprite_frame_panel.invalidate_thumbnails()
        self._update_window_title()
        self._refresh_workspace_tabs()
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
        maximum = (
            1024
            if operation
            in {
                SelectionAdjustment.GROW,
                SelectionAdjustment.SHRINK,
            }
            else 256
        )
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
            self._show_operation_status(f"已取消{label}")
            return
        if outcome.status is JobStatus.FAILED:
            self._show_operation_status(f"{label}失败：{outcome.error}")
            return
        if not isinstance(outcome.value, SelectionOperationResult):
            self._show_operation_status(f"{label}失败：无效的任务结果")
            return
        result = outcome.value
        document = self.controller.document
        if (
            document is None
            or document.id != result.document_id
            or document.revision != result.revision
        ):
            self._show_operation_status(f"文档已变化，已丢弃过期{label}结果")
            return
        if not self.controller.commands.apply_selection_tiles(result.updates, label):
            self._show_operation_status(f"{label}未产生变化")

    def _close_selection_progress(self) -> None:
        if self._selection_progress is not None:
            self._selection_progress.close()
            self._selection_progress.deleteLater()
            self._selection_progress = None

    def _update_selection_actions(self) -> None:
        document = self.controller.document
        has_document = document is not None
        writable = has_document and not self._active_surface_is_linked()
        active = has_document and document.selection.is_active if document is not None else False
        self.select_all_action.setEnabled(writable and not self._editing_busy)
        self.crop_to_selection_action.setEnabled(writable and active and not self._editing_busy)
        self.clear_selected_pixels_action.setEnabled(
            writable and active and not self._editing_busy
        )
        self.tool_options_panel.set_selection_active(
            writable and active and not self._editing_busy
        )
        for action in (
            self.deselect_action,
            self.invert_selection_action,
            self.feather_selection_action,
            self.grow_selection_action,
            self.shrink_selection_action,
            self.smooth_selection_action,
        ):
            action.setEnabled(writable and active and not self._editing_busy)

    def _update_undo_actions(self, *_args: object) -> None:
        if hasattr(self, "color_adjustment_controller") and self.color_adjustment_controller.active:
            self.undo_action.setEnabled(False)
            self.redo_action.setEnabled(False)
            self.undo_action.setText("撤销")
            self.redo_action.setText("重做")
            return
        tool = self._tools.get("transform") if hasattr(self, "_tools") else None
        if isinstance(tool, TransformTool) and tool.has_active_session:
            undo_text = tool.preview_undo_text
            redo_text = tool.preview_redo_text
            self.undo_action.setEnabled(tool.can_undo_preview)
            self.redo_action.setEnabled(tool.can_redo_preview)
            self.undo_action.setText(f"撤销 {undo_text}" if undo_text else "撤销")
            self.redo_action.setText(f"重做 {redo_text}" if redo_text else "重做")
            return
        shape_tool = self._tools.get("shape") if hasattr(self, "_tools") else None
        if isinstance(shape_tool, ShapeTool) and shape_tool.has_active_session:
            self.undo_action.setEnabled(shape_tool.can_undo_preview)
            self.redo_action.setEnabled(shape_tool.can_redo_preview)
            self.undo_action.setText("撤销 图形调整")
            self.redo_action.setText("重做 图形调整")
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
        shape_tool = self._tools.get("shape")
        if isinstance(shape_tool, ShapeTool) and shape_tool.has_active_session:
            shape_tool.undo_preview()
            return
        self.controller.commands.undo_stack.undo()

    def _redo_requested(self, _checked: bool = False) -> None:
        _ = _checked
        tool = self._tools.get("transform")
        if isinstance(tool, TransformTool) and tool.has_active_session:
            tool.redo_preview()
            return
        shape_tool = self._tools.get("shape")
        if isinstance(shape_tool, ShapeTool) and shape_tool.has_active_session:
            shape_tool.redo_preview()
            return
        self.controller.commands.undo_stack.redo()

    def _update_window_title(self) -> None:
        document = self._project_document()
        if document is None:
            self.setWindowTitle(f"EasyPicture {__version__}")
            return
        self.setWindowTitle(f"{self._project_display_label(document)} — EasyPicture")

    def _on_zoom_changed(self, zoom: float) -> None:
        self.zoom_label.setText(f"{zoom * 100:.1f}%")

    def _on_cursor_position(self, point: Point | None) -> None:
        if point is None:
            self.coordinate_label.setText("x: —  y: —")
        else:
            self.coordinate_label.setText(f"x: {int(point.x)}  y: {int(point.y)}")

    def closeEvent(self, event: QCloseEvent) -> None:
        self._cancel_color_adjustment_for_lifecycle()
        if not self.controller.request_exit(self):
            event.ignore()
            return
        self.settings.save_window(self)
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if hasattr(self, "color_adjustment_controller") and self.color_adjustment_controller.active:
            if event.key() == Qt.Key.Key_Escape:
                if self.color_adjustment_controller.sampling:
                    self.color_adjustment_controller.cancel_sampling()
                else:
                    self.color_adjustment_controller.cancel()
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._apply_color_adjustment()
                event.accept()
                return
        super().keyPressEvent(event)


__all__ = ["EmptyStateWidget", "MainWindow"]
