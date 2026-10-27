# P6B 裁切、画布/图像/图层尺寸验收

- 日期：2026-07-28
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted

## 已实现

- 裁切工具：画布外扩展、八手柄、移动、比例预设、自定义比例、三分网格、数值坐标；
- 裁切手势右键恢复本次拖动，Enter 应用，Esc 或取消按钮退出；
- 调整画布大小：宽高、九点锚点、实时完整帧预览；
- 缩放图像：宽高联动、四种插值、全部图层、origin 和选区同步缩放；
- 缩放图层：四种插值，选区不变，锁定层拒绝；
- 图层固定与画布同尺寸、origin 固定为 `(0, 0)`；
- 裁切或缩小会删除画布外数据，撤销可恢复，后续扩展只补透明像素；
- 裁切透明像素：按全部图层 Alpha 非零范围裁切；
- 裁切到选区；
- Delete 按活动选区权重降低 Alpha，并保留隐藏 RGB；
- 画笔/油漆桶正常、替换、正片叠底、滤色和叠加模式；
- 紧凑颜色 Dock 与中文 Blender 风格 HSV 圆盘、明度条、RGB/HSV/Hex 分页颜色弹窗；
- 图层列表 48×48 透明棋盘格缩略图及按 revision 精确刷新；
- 文档尺寸、图层几何、像素和选区的一次性事务及单步 undo/redo；
- JobRunner 取消、revision 过期校验、1 GiB PatchStore 预算沿用。

## 自动验收

实现切片目前覆盖：

- 九点锚点偏移；
- 负坐标裁切与透明扩展；
- 缩小画布后画布外像素删除，撤销完整恢复；
- 图像和图层缩放及选区语义；
- 固定画布图层归一化、透明 Alpha 内容边界及旧项目兼容；
- 原子撤销、过期结果拒绝和锁定层拒绝；
- 几何完整帧预览更新及清理；
- 裁切工具数值、拖动、右键恢复、取消和单步提交；
- 尺寸对话框宽高联动、锚点与插值。

最终检查结果：

```text
uv run ruff check .                         PASS
uv run mypy src                             PASS (66 source files)
P6B 与受影响回归测试                        PASS (155 tests)
颜色弹窗定向测试                            PASS (32 tests)
图层缩略图定向与受影响回归                  PASS (37 tests)
uv run python -m easypicture --smoke-test   PASS
source I/O smoke                            PASS
Nuitka standalone build                     PASS
isolated PATH startup smoke                 PASS
isolated PATH I/O smoke                     PASS
standalone software render smoke            PASS
standalone OpenGL render smoke              PASS
```

本轮按修改范围执行回归，没有运行完整 pytest 或性能基准。

构建产物：`build/nuitka/easypicture.dist/EasyPicture.exe`。

## 人工验收

1. `Passed`：裁切框移动、八手柄、画布外透明扩展、比例和三分网格；
2. `Passed`：裁切应用/取消、右键恢复当前拖动及单步撤销；
3. `Passed`：九点锚点扩大/缩小画布，确认裁掉内容仅能通过撤销恢复；
4. `Passed`：图像缩放四种插值、宽高联动和实时预览；
5. `Passed`：图层缩放、裁切透明像素、Delete 清除选区及绘制模式；
6. `Passed`：带选区裁切/缩放，保存重开 `.limg`；
7. `Passed`：中文颜色选择弹窗的 HSV 圆盘、明度条、RGB/HSV/Hex 分页、连续数值拖动
   与模态屏幕吸管。

验收期间修复了颜色模型联动回写中断活动数值拖动的问题，并将吸管改为由模态颜色弹窗
直接捕获输入，消除了无父级透明窗口导致的 Windows 输入阻断。
