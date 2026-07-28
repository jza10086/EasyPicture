# P6A 自由变换与浮动选区验收

- 日期：2026-07-27
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted

## 交付结果

- 已实现 Qt 无关仿射/投影矩阵、移动、单边缩放、规则角点缩放、旋转和不规则四角编辑；
- 已实现活动层与活动选区两种变换来源；
- 已实现选中像素剪切浮动、羽化权重和选区同步变换；
- 已实现四种插值、预乘 Alpha 重采样和 Alpha 0 隐藏 RGB 保留；
- 已实现 256 Tile 输出、取消检查及 64 MiB 以上临时 memmap；
- 已实现图层 size/origin、像素和选区的单命令原子撤销；
- P6R 已用统一完整帧协调器取代普通合成缓存、静态缓存和 LOD 图集；
- 自由变换拖动期间仅更新 GPU 逆映射矩阵，不重新生成目标合成 Tile；
- OpenGL/软件后端每次绘制最新完整 viewport，不依赖旧 framebuffer；
- 已实现八个边/角手柄、四个外置旋转手柄、中心点、键盘移动和 Enter/Esc；
- 已启用属性面板的四角精确坐标，并修复数值连续拖动被参数回写中断；
- 已实现正在变换的图层被删除时安全退出会话；
- 已实现 Blender 风格浮点表达式输入及变换选项持久化；
- 已实现自由变换会话内分步撤销/重做，最终提交仍保持一条文档命令；
- 已实现拖动期间右键撤销当前拖动、旋转图标顺时针调整 90°；
- 中心点精确坐标已移入属性面板；
- 全局设置可独立调整普通与 Shift 数值拖动灵敏度；
- 已新增 ADR-0016、ADR-0017 和取代旧渲染规则的 ADR-0018。

独立透视工具、网格变形、翻转、裁切、画布/图像尺寸和图形工具未进入 P6A。

## 自动验收范围

- 仿射/投影矩阵组合、逆变换、四角映射、旋转边界和奇异矩阵拒绝；
- 最近邻整数移动、隐藏 RGB、半透明选区和选区随动；
- 复合撤销、过期结果、撤销预算和 `.limg` 往返；
- 预览层级、generation 合并、普通缓存隔离、LOD 复用、删除图层退出和连续数值拖动；
- P2 渲染、P4 工具 UI、P5 选区 UI 相关回归。

```text
uv run ruff check <P6A 及受影响模块>       PASS
uv run mypy src                           PASS (60 source files)
uv run pytest -q <P6A 定向回归>           PASS (106 tests)
uv run python -m easypicture --smoke-test PASS
Nuitka standalone build                    PASS
isolated PATH standalone smoke             PASS
isolated PATH standalone I/O smoke         PASS
```

4096×4096 密集单层、25% 缩放的预览图集探测：无既有源缓存时首次约 99 ms，
正常已有源 Tile 缓存时首次约 31 ms，同一会话后续绘制约 3 ms。该数据用于验证
会话级 LOD 复用，不作为极端项目阻塞性指标。

Nuitka 4.1.3 在 Python 3.14 下的实验性支持警告保持不变；构建产物位于
`build/nuitka/easypicture.dist/EasyPicture.exe`。

## 人工验收

1. `Passed`：整层移动、单边/规则缩放、旋转和不规则四角编辑；
2. `Passed`：八个边/角手柄、四个旋转按钮、中心点、修饰键和精确数值栏；
3. `Passed`：二值与羽化选区剪切后随像素变换；
4. `Passed`：多图层遮挡、缩放和平移期间的实时预览；
5. `Passed`：Esc 字节不变及一次提交一次撤销；
6. `Passed`：画布外像素保存重开；
7. `Passed`：较大图层提交进度、取消和窗口响应。

人工验收后的交互改进（会话内分步撤销、拖动右键复位、图标方向、中心点布局和
数值灵敏度设置）已完成自动定向检查和人工复核。

## 复现命令

```powershell
uv run pytest -q tests/unit/test_transform.py tests/unit/test_tile_renderer.py `
  tests/ui/test_p6_transform_ui.py tests/ui/test_p4_tool_ui.py `
  tests/ui/test_p5_selection_ui.py
uv run python -m easypicture --smoke-test
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
