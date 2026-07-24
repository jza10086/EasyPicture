# P5 统一选区系统验收

- 日期：2026-07-24
- 自动验收：Passed
- 人工验收：Pending
- 总体状态：Ready for manual acceptance

## 交付结果

- 已实现选区 Tile 事件、精确提交、modified/revision 和统一撤销补丁；
- 已实现替换、添加、减去和相交的灰度蒙版数学；
- 已实现矩形选区、全选、取消选择和反选；
- 已实现魔棒连续/非连续、4/8 邻域、当前层/合并取样及 Alpha 比较开关；
- 已实现羽化、扩展、收缩和平滑；
- 已实现独立于工具 Overlay 的持久蚂蚁线；
- 已实现选区参数 QSettings 持久化、恢复草稿触发和 `.limg` v1 往返；
- 已新增 ADR-0014 和 ADR-0015。

套索、快速蒙版、选区移动、浮动选区和基于选区的变换未进入 P5。

## 自动验收

```text
uv lock --check                         PASS
uv run ruff check .                     PASS
uv run mypy src                         PASS (54 source files)
uv run pytest -q                        PASS (169 tests)
uv run python -m easypicture --smoke-test
                                        PASS
uv run python -m easypicture --io-smoke-test <dir>
                                        PASS
```

新增测试覆盖模糊集合组合、跨 Tile 矩形、全选/取消/反选、选区命令 clean state、
Alpha 比较开关、羽化结果、修饰键优先级、参数持久化、持久蚂蚁线及恢复草稿往返。
P0～P4 原有测试继续通过。

## 性能验收

| 指标 | 结果 | 状态 |
| --- | ---: | --- |
| 4096 全画布魔棒 | 666.519 ms | PASS |
| 4096、16 px 羽化 | 43.696 ms | PASS |
| 进程 RSS 净增量 | 1.89 MiB | RECORDED |

两项正式算法均运行在 JobRunner Worker 中；主线程只提交快照和最终 Tile 补丁。
详见 [`docs/benchmarks/p5_selection.md`](benchmarks/p5_selection.md)。

## Standalone 验收

- Nuitka 4.1.3、MSVC 14.5 构建成功；
- `EasyPicture.exe` 约 22.78 MiB；
- standalone 目录约 224.39 MiB，共 81 个文件；
- PATH 仅保留 Windows 系统目录时，窗口 smoke 与 `.limg`/PNG I/O smoke 均返回 0；
- Nuitka 对 Python 3.14 的实验性支持警告保持不变。

## 人工验收

1. `Pending`：矩形选区四种模式及 Shift/Alt 临时模式；
2. `Pending`：全选、取消、反选和撤销重做；
3. `Pending`：蚂蚁线在缩放、平移和工具切换时保持正确；
4. `Pending`：魔棒容差、Alpha 开关、连续模式、邻域和合并取样；
5. `Pending`：羽化、扩展、收缩和平滑；
6. `Pending`：画笔、橡皮和油漆桶受半透明选区约束；
7. `Pending`：保存重开、modified 标题和恢复草稿。

## 复现命令

```powershell
.\scripts\check.ps1
.\scripts\benchmark-p5.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
