# P3 图片导入、项目保存与自动恢复验收

- 日期：2026-07-23
- 自动验收：Passed
- 人工验收：Accepted with deferred performance scenario
- Python：3.14.3 x64
- 项目 schema：`.limg` v1
- 恢复防抖：60 秒

## 交付结果

- 已实现 PNG/JPEG/WebP/BMP/TIFF/TGA 导入、EXIF Orientation 和 ICC→sRGB；
- 已实现普通图片 clean 文档身份、项目路径和文件名窗口标题；
- 当时实现 `.limg` v1 稀疏 RGBA/selection Tile、缩略图、迁移入口和完整校验；
  预发布收敛后迁移入口已删除，Tile 安全校验与原子写入继续保留；
- 已实现同目录临时文件、flush/fsync、重读复核及原子替换；
- 已实现打开、保存、另存为、最小 PNG 导出和保存/放弃/取消生命周期；
- 已实现无 Qt 权威 source-over 合成和大画布临时 memmap；
- 已实现串行可取消 JobRunner、模态进度和 revision clean 提交；
- 已实现 60 秒自动恢复、启动恢复/丢弃和损坏草稿隔离；
- 已新增 ADR-0010、ADR-0011、P3 密集项目基准和 I/O smoke。

画笔、选区操作、完整格式导出、最近文件和高级元数据编辑未进入 P3。

## 自动验收

```text
uv lock --check                         PASS
uv run ruff check .                     PASS
uv run mypy src                         PASS (41 source files)
uv run pytest -q                        PASS (119 tests)
uv run python -m easypicture --smoke-test
                                        PASS
uv run python -m easypicture --io-smoke-test <dir>
                                        PASS
```

测试覆盖项目往返、Unicode、图层属性、selection、Alpha 0/RGB 非 0、常用导入
格式、EXIF、ICC 回退、多帧首帧策略、PNG modified 边界、原子失败/取消、路径穿越、
Windows 绝对路径、未来 schema、校验和、解码预算、JobRunner、三态生命周期和恢复
草稿。P0～P2 原有 87 项测试继续通过。

## 性能验收

4096×4096、10 个密集层的实测结果：

| 指标 | 结果 | 状态 |
| --- | ---: | --- |
| Snapshot 主线程耗时 | 1.543 ms | PASS |
| `.limg` 保存及完整复核 | 8700.060 ms | RECORDED |
| `.limg` 重开 | 907.899 ms | RECORDED |
| 取消响应 | 16.682 ms | PASS |
| 权威像素 / Tile | 640 MiB / 2560 | PASS |

详见 [`docs/benchmarks/p3_persistence.md`](benchmarks/p3_persistence.md)。

## Standalone 验收

- Nuitka 4.1.3、MSVC 14.5 构建成功；
- `EasyPicture.exe` 约 21.95 MiB；
- standalone 目录约 125.26 MiB，共 79 个文件；
- 包含 Qt platforms/imageformats、Pillow 和 ImageCms 运行依赖；
- PATH 仅保留 Windows 系统目录时，窗口 smoke 与 `.limg`/PNG I/O smoke 均返回 0。

Nuitka 仍提示 Python 3.14 支持属于实验性。两次 clcache 替换首次被占用，但自动重试
成功，不影响生成物；依赖或杀毒软件策略变化后应重新执行完整构建。

## 人工验收

1. 导入普通图片及带 EXIF 旋转的照片：Passed；
2. 保存 `.limg`，修改后 Ctrl+S 并重开检查图层和像素：Passed；
3. 验证保存/放弃/取消及保存失败分支：Passed；
4. 导出透明 PNG，确认 modified 星号不消失：Passed；
5. 大项目保存期间确认进度、取消和窗口响应：Deferred（本机暂无代表性大项目，
   且极端项目性能不作为轻量级工具后续阶段的阻塞条件）；
6. 修改后等待 60 秒，强制结束程序并重启恢复：Passed；
7. 检查新建、普通图片和项目文件三种标题：Passed。

## 复现命令

```powershell
.\scripts\sync.ps1
.\scripts\check.ps1
.\scripts\benchmark-p3.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
