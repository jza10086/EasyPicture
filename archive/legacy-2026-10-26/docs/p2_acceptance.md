# P2 图层合成、图层管理与撤销骨架验收

- 日期：2026-07-23
- 自动验收：Passed
- 人工验收：Passed
- Python：3.14.3 x64
- Tile：256×256
- 渲染缓存预算：256 MiB
- 撤销预算：1 GiB / 100 条命令

## 交付结果

- 已固定 Document 图层顺序为底层到顶层，并实现完整图层变更事件；
- 已实现新建、删除、复制、重命名、排序、显隐、不透明度及两种锁定；
- 已实现共享 Tile 复制及后续 copy-on-write；
- 已实现精确 Tile 替换、缺失 Tile 恢复和像素 dirty 映射；
- 已实现 `DocumentCommandService`、持久 `QUndoStack` 和压缩
  `UndoPatchStore`；
- 已实现 1 GiB 预算的确认、取消、历史重置和单命令拒绝分支；
- 已实现 QPainter source-over、两级 LRU、精确失效和渐进调度；
- 已将 CanvasItem 接入实际多图层渲染；
- 已实现顶层优先的图层面板、拖放与按钮排序及动态撤销/重做 Action；
- 已修复显隐双击被重命名触发器吞掉的问题，并实现类似 Blender 的跨行拖拭
  批量显隐；眼睛区域不选择或移动图层，只有文字区域参与选择、重命名和拖动排序；
- 已新增 ADR-0008、ADR-0009 和 P2 密集层基准。

保存、另存为和导出继续禁用。P2 没有加入画笔、选区工具、导入导出、项目文件或
后台 JobRunner。

## 自动验收

```text
uv lock --check                         PASS
uv run ruff check .                     PASS
uv run mypy src                         PASS (32 source files)
uv run pytest -q                        PASS (87 tests)
uv run python -m easypicture --smoke-test
                                        PASS
Nuitka standalone build                 PASS
isolated PATH standalone smoke          PASS
```

测试在原有 58 项 P0/P1 测试基础上覆盖：

- 图层创建、复制、删除、重命名、排序、属性和最后一层保护；
- 顶层优先显示、拖放及按钮排序、选择和 Action 状态；
- 显隐区域连续单击/双击、跨行拖拭、活动层隔离及单步撤销；
- 连续 100 次图层属性 undo/redo；
- 共享 Tile 的 copy-on-write；
- 多 Tile before/after、缺失 Tile 和 PatchStore raw/zlib 往返；
- 图层锁定和透明像素锁定；
- 预算确认、取消、单命令超限和句柄释放；
- clean index 恢复和 clean 基线失效；
- 半透明、Alpha 0/RGB 非 0、显隐、不透明度；
- 正负、非 Tile 对齐 origin 和局部图层边界；
- 像素局部失效、属性全合成失效及有效源缓存保留；
- 渐进调度期间的 Qt 事件循环响应；
- 领域模型无 Qt/QImage 依赖。

## 性能验收

实际场景为 4096×4096、10 个全尺寸密集图层，共 2560 个 Tile 和 640 MiB 权威
像素。

| 门槛 | 结果 | 状态 |
| --- | ---: | --- |
| 10 层冷缓存单 Tile P95 ≤ 16 ms | 2.250 ms | PASS |
| source-over golden 误差 ≤ 1 LSB | 0 LSB | PASS |
| 渐进渲染连续阻塞 < 100 ms | 22.760 ms | PASS |
| 渲染缓存 ≤ 256 MiB | 256 MiB | PASS |
| 实际 4096×4096、10 密集层 | 640 MiB / 2560 Tile | PASS |

256 个文档 Tile 渐进生成总耗时为 1714.314 ms；总耗时不是单次 UI 阻塞。完整
结果见
[`docs/benchmarks/p2_layer_composition.md`](benchmarks/p2_layer_composition.md)。

## Standalone 验收

- Nuitka 4.1.3、MSVC 14.5 构建成功；
- `EasyPicture.exe` 约 16.78 MiB；
- standalone 目录约 109.38 MiB，共 74 个文件；
- 包含 `qwindows.dll` 和 Qt imageformats；
- PATH 仅保留 Windows 系统目录时执行 `--smoke-test`，退出码为 0。

Nuitka 仍提示 Python 3.14 支持属于实验性。当前构建和运行通过；依赖升级后必须
重新执行 standalone 构建和隔离 PATH 冒烟。

## 人工验收

以下项目已由用户在 2026-07-23 验收通过；图层显隐连续点击修复和类似 Blender
的拖拭批量显隐也已确认符合预期。

1. 新建、删除、复制和双击重命名图层；
2. 使用拖放及上移/下移按钮调整图层顺序；
3. 检查显隐、不透明度、图层锁定和透明像素锁定；
4. 检查空白多层状态下的棋盘格及各图层属性显示；
5. 连续执行撤销/重做，确认菜单文字、快捷键和窗口标题；
6. 撤销回新建状态后确认文档恢复 clean，图层修改后关闭会提示；
7. 缩放、平移和适合窗口时确认 Tile 渐进出现且界面无长时间冻结。

像素补丁和透明像素锁定的实际像素效果在 P2 没有正式编辑工具入口，由自动 golden
测试负责；画笔阶段会再次加入人工操作验收。

## 复现命令

```powershell
.\scripts\sync.ps1
.\scripts\check.ps1
.\scripts\benchmark-p2.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
