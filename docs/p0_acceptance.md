# P0 架构验证与工程初始化验收

- 日期：2026-07-23
- 状态：Passed
- Python：3.14.3 x64
- Tile 决策：256×256

## 交付结果

- 已初始化 `main` 分支 Git 仓库；
- 已建立 uv 工程、Python 3.14 约束和 `uv.lock`；
- 已建立最小 PySide6 窗口及 `--smoke-test` 命令；
- 已实现稀疏 copy-on-write `TilePixelStore`；
- 已实现 straight RGBA → owned premultiplied QImage 桥；
- 已建立 Ruff、mypy、pytest、pytest-qt 和 PowerShell 脚本；
- 已完成五份架构决策记录；
- 已完成 256/512、4096/8192、QImage 和 Alpha 合成基准；
- 已完成 Nuitka standalone 构建和隔离 PATH 冒烟。

P0 没有加入画笔、图层面板、项目文件或其他正式编辑功能。

## 质量验证

```text
uv lock --check                         PASS
uv run ruff check .                     PASS
uv run mypy src                         PASS
uv run pytest -q                        PASS (21 tests)
uv run python -m easypicture --smoke-test
                                        PASS
```

测试覆盖：

- 缺失 Tile 的透明与只读行为；
- copy-on-write 快照稳定性和 revision；
- 跨 2、4、9 Tile 的区域读写；
- 错误 dtype、通道数和非连续数组；
- 全透明、全不透明和半透明 QImage；
- Alpha 为 0、RGB 非 0 的权威数据；
- NumPy 源修改/释放后的 QImage 生命周期；
- 半透明 source-over 合成 1 LSB 容差；
- 子进程模块入口冒烟。

## 基准验收

| 门槛 | 结果 | 状态 |
| --- | ---: | --- |
| 256 Tile QImage 转换 P95 ≤ 5 ms | 0.183 ms | PASS |
| Alpha 合成最大误差 ≤ 1 LSB | 1 LSB | PASS |
| 4096 密集层实际分配 | 64 MiB | PASS |
| 8192 密集层实际分配 | 256 MiB | PASS |
| Tile 规则自动决策 | 256 | PASS |

完整结果见
[`docs/benchmarks/p0_tile_benchmark.md`](benchmarks/p0_tile_benchmark.md)。

## Standalone 验收

构建参数：

```text
--mode=standalone
--python-flag=-m
--enable-plugin=pyside6
--msvc=latest
--assume-yes-for-downloads
--windows-console-mode=disable
--output-filename=EasyPicture.exe
```

实测：

- MSVC 14.5；
- `EasyPicture.exe` 约 6.71 MiB；
- standalone 目录约 73.06 MiB，共 56 个文件；
- 包含 `qwindows.dll` 和 Qt imageformats；
- 将 PATH 限制为 Windows 系统目录后运行 `--smoke-test`，退出码为 0。

Nuitka 4.1.3 会提示 Python 3.14 支持仍属实验性。当前构建和运行均通过，但该提示
作为已知上游风险保留；依赖升级必须重新执行 standalone 冒烟。

## 复现命令

```powershell
.\scripts\sync.ps1
.\scripts\check.ps1
.\scripts\benchmark.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```
