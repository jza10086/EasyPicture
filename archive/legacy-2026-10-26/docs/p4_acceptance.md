# P4 画笔、橡皮、吸管与油漆桶验收

- 日期：2026-07-24
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted
- 输入范围：鼠标
- 画笔大小：拖动 1～1024 px，手动输入 1～8192 px
- 区域容差：straight RGBA 最大通道差

## 交付结果

- 已实现私有工作 Tile、正确图层顺序的实时预览和一次笔划一次提交；
- 已实现 stale Tile 上一帧保留，像素及图层状态渐进重绘期间不回退到棋盘格；
- 已实现圆形画笔的大小、硬度、不透明度、间距和抗锯齿；
- 已实现只降低 Alpha 且保留隐藏 RGB 的橡皮；
- 已实现可见合成/活动层吸管，左右键分别设置前景/背景色；
- 已实现 RGBA 容差、连续/非连续、4/8 邻域和合并取样油漆桶；
- 已实现 0～8 px 油漆桶边缘覆盖，默认在半透明画笔边缘后方扩展 1 px；
- 已实现后台可取消填充、延迟进度对话框及 revision 过期结果拒绝；
- 已实现稀疏 selection 权重预留、图层锁定和透明像素锁定；
- 已实现 B/E/I/G 工具、动态选项栏、圆形画笔光标和参数持久化；
- 已实现 viewport 同步画笔轮廓、中键/Space 平移后的工具光标恢复，以及滚轮滚动前擦除、滚动后重绘；
- 已实现任意缩放下将画布四边拖至视口中心；右侧栏与图层、颜色及属性卡片采用
  不同底色，并具有独立外边距、内容留白及 Dock 间隔；
- 已根据 Blender 编辑区层级统一应用深色主题，右侧栏改为连续暗色面板、紧凑标题、
  细分隔线和蓝色图层选中态；
- 已将顶部整数输入改为横向拖动数值滑条，按默认范围显示蓝色填充并移除右侧按钮；
  画笔及橡皮轮廓在文档边界外的 viewport 工作区仍保持可见；
- 已新增 ADR-0012、ADR-0013 和 P4 固定场景基准。

数位笔压感、倾斜、笔速、自定义笔尖、选区 UI、形状、文本和滤镜未进入 P4。

## 自动验收

```text
uv lock --check                         PASS
uv run ruff check .                     PASS
uv run mypy src                         PASS (48 source files)
uv run pytest -q                        PASS (151 tests)
uv run python -m easypicture --smoke-test
                                        PASS
uv run python -m easypicture --io-smoke-test <dir>
                                        PASS
```

新增测试覆盖 dab 插值、跨 9 Tile、透明合成、隐藏 RGB、选区权重、RGBA 容差、
4/8 邻域、连续/非连续区域、活动层/合并取样、取消、过期会话、预览图层顺序、
一笔一命令、零值稀疏选区及工具设置持久化。P0～P3 原有 119 项测试继续通过。

深色主题增量修改按风险执行针对性验证：主窗口、图层面板及工具 UI 共 16 项测试通过，
所改 4 个源码文件通过 Ruff/mypy，源码 smoke 返回 0；未重复全量测试及 standalone 构建。

顶部拖动输入与画笔轮廓增量修改执行 9 项相关测试，并对所改源码运行 Ruff/mypy；
按增量交付策略不重复全量测试。

## 性能验收

| 指标 | 结果 | 状态 |
| --- | ---: | --- |
| 32 px 画笔事件 P95 | 8.543 ms | PASS |
| 128 px 画笔事件 P95 | 5.280 ms | PASS |
| 256 px 画笔事件 P95 | 21.748 ms | PASS |
| 4096×4096 全画布填充 | 4785.892 ms | RECORDED |
| 填充补丁 | 256 Tile / 64 MiB | PASS |

详见 [`docs/benchmarks/p4_editing.md`](benchmarks/p4_editing.md)。

## Standalone 验收

- Nuitka 4.1.3、MSVC 14.5 构建成功；
- `EasyPicture.exe` 约 22.27 MiB；
- standalone 目录约 223.88 MiB，共 81 个文件；
- 已包含 P4 首次实际调用的 OpenCV 运行库；
- PATH 仅保留 Windows 系统目录时，窗口 smoke 与 `.limg`/PNG I/O smoke 均返回 0。

Nuitka 仍提示 Python 3.14 支持属于实验性。OpenCV 被正式纳入运行闭包后，目录体积
相对 P3 增加；这属于 standalone 依赖成本，不影响程序运行时的轻量项目内存目标。

## 人工验收结果

1. `Passed`：不同大小、硬度、透明度和间距的快速连续笔划；
2. `Passed`：橡皮显示棋盘格且不会留下白色；
3. `Passed`：吸管左右键及可见结果/当前层取样；
4. `Passed`：油漆桶容差、连续模式、8 邻域和合并取样；
5. `Passed`：锁定、透明像素锁定和一笔一次撤销；
6. `Passed`：缩放、平移及多图层遮挡下的实时预览；
7. `Passed`：重启后工具参数恢复；
8. `Passed`：Blender 风格深色主题、顶部拖动数值输入和画布外画笔轮廓。

## 复现命令

```powershell
.\scripts\check.ps1
.\scripts\benchmark-p4.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
