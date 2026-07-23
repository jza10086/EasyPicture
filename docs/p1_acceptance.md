# P1 应用骨架与文档模型验收

- 日期：2026-07-23
- 自动验收：Passed
- 人工验收：Pending
- Python：3.14.3 x64
- Tile 决策：256×256（保持不变）

## 交付结果

- 已实现不依赖 Qt 的 `Document`、`RasterLayer`、`Selection` 和同步领域事件；
- 已实现稀疏 copy-on-write 单通道 `MaskTileStore`；
- 已实现单窗口、单文档生命周期和可替换对话框服务；
- 已实现新建、关闭、退出及 modified 文档的放弃/取消分支；
- 已实现 `QGraphicsView` 画布、鼠标锚点缩放、中键与 Space 平移；
- 已冻结全局、viewport、scene、document、layer 坐标转换链；
- 已实现 `Tool`、`ToolContext`、`ToolController`、`OverlayManager` 和
  `NullTool`；
- 已实现前景色、背景色、交换颜色和 `QSettings` 持久化；
- 已建立主菜单、工具选项栏、工具栏、Dock、状态栏和窗口标题状态；
- 已新增 ADR-0006 和 ADR-0007。

P1 没有加入图层合成、撤销栈、文件读写、图层操作或正式编辑工具。保存、另存为
和导出入口保持禁用。

## 自动验收

```text
uv lock --check                         PASS
uv run ruff check .                     PASS
uv run mypy src                         PASS (27 source files)
uv run pytest -q                        PASS (57 tests)
uv run python -m easypicture --smoke-test
                                        PASS
Nuitka standalone build                 PASS
isolated PATH standalone smoke          PASS
```

57 项测试包括原有 21 项 P0 测试，并新增：

- Document 尺寸边界、默认图层、UUID、revision、modified 和事件订阅；
- Mask 稀疏存储、copy-on-write 及跨 2、4、9 Tile 读写；
- clean/modified 文档的新建、关闭、退出及取消分支；
- 取消新建不替换当前文档；
- 临时目录中的设置、窗口状态和颜色往返；
- 缩放、平移和非零图层 origin 下的坐标往返；
- 鼠标锚点缩放、中键平移和画布视图不修改领域 revision；
- 工具连续切换时 Overlay、交互状态和光标清理；
- 主窗口空状态、标题、菜单启用状态和状态栏。

## 性能回归

| 门槛 | 结果 | 状态 |
| --- | ---: | --- |
| 256 Tile QImage 转换 P95 ≤ 5 ms | 0.143 ms | PASS |
| Alpha 合成最大误差 ≤ 1 LSB | 1 LSB | PASS |
| 4096 密集层实际分配 | 64 MiB | PASS |
| 8192 密集层实际分配 | 256 MiB | PASS |
| Tile 规则自动决策 | 256 | PASS |

512 Tile 的 4096 全画布遍历耗时仅比 256 低约 1.23%，局部编辑综合 P95
约为 256 的 4.21 倍，未满足 ADR-0003 的切换门槛，因此继续固定 256 Tile。

## Standalone 验收

- 使用 Nuitka 4.1.3 和 MSVC 14.5 构建成功；
- `EasyPicture.exe` 约 16.31 MiB；
- standalone 目录约 108.92 MiB，共 74 个文件；
- 包含 `qwindows.dll` 和 Qt imageformats；
- 将 PATH 限制为 Windows 系统目录后执行 `--smoke-test`，退出码为 0。

Nuitka 仍提示 Python 3.14 支持属于实验性。当前构建和运行均通过；依赖升级后必须
重新执行 standalone 构建与隔离 PATH 冒烟。

## 待人工验收

1. 新建默认 1920×1080 和自定义尺寸文档；
2. 检查棋盘格、Dock、状态栏和窗口标题；
3. 测试缩放、100%、适合窗口、中键和 Space 平移；
4. 测试前景色、背景色和交换颜色；
5. 重启后确认窗口布局、颜色和上次新建尺寸恢复；
6. 确认关闭 clean 文档不提示；modified 对话框分支已由自动测试覆盖。

人工验收完成后，将本文件的人工验收状态改为 `Passed`。P1 自动验收通过不代表
上述交互体验已经得到用户确认。

## 复现命令

```powershell
.\scripts\sync.ps1
.\scripts\check.ps1
.\scripts\benchmark.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
