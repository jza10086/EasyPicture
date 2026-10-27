# 数值输入隔离测试环境

该环境只加载 `DragValueSpinBox`、`DragValueDoubleSpinBox` 和 Qt 事件循环，不创建
EasyPicture 主窗口、文档、画布、工具、设置或渲染后端。

## 启动

人工操作窗口：

```powershell
.\scripts\run-numeric-input-harness.ps1
```

自动运行并以退出码报告结果：

```powershell
.\scripts\run-numeric-input-harness.ps1 -AutoTest
```

## 人工验证

1. 单击数值框并释放，应从 `idle → wait_drag → text_editing`，且文本仅在首次进入时全选。
2. 在文本状态再次按下，必须进入 `text_selecting`；移动和释放只能返回
   `text_editing`，不得经过 `wait_drag` 或 `dragging`。
3. 在文本内连续点击、双击和拖选，检查插入点与选择行为。
4. 按 `Esc` 退出文本状态；此后从 `idle` 按住并水平移动，才允许进入
   `wait_drag → dragging → idle`。
5. 任何 `text_editing/text_selecting → wait_drag/dragging` 会在日志中以 `!!!`
   标记，并增加“阻断失败”计数。

日志同时记录事件类型、实际接收事件的 Qt 对象、按键/鼠标状态、全局坐标和状态迁移，
用于区分事件被投递给整数外壳、浮点包装还是内部 `QLineEdit`。

## 自动序列

整数和浮点控件各执行 15 项检查：

- 首次点击进入文本；
- 首次进入全选；
- 将同一框内点击先投递给顶层窗口，模拟 Windows/Qt 原生事件分发阶段；
- 将二次按下直接投递给控件外壳；
- 二次拖选阻断数值拖动；
- 将拖选投递给内部编辑器；
- `Esc` 退出文本；
- 仅空闲状态允许水平数值拖动。

该测试环境保留为开发诊断工具，不进入正式应用菜单，也不随 `.limg` 保存。
