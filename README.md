# EasyPicture

EasyPicture 是一个面向 Windows 的本地离线轻量位图编辑器。当前仓库已完成 P3
可行走骨架：包含单文档生命周期、分块像素/选区存储、多图层管理、渐进 Tile 合成、
图层与像素撤销、常用图片导入、`.limg` 项目保存/恢复、自动恢复和 PNG 导出。

画笔、选区操作、变换和完整格式导出尚未实现。

## 环境

- Windows x64
- Python 3.14
- uv
- Visual Studio Build Tools（Nuitka standalone）

所有脚本会把 uv 缓存放在仓库内被忽略的 `.cache/uv`。

## 常用命令

```powershell
.\scripts\sync.ps1
.\scripts\run.ps1
.\scripts\check.ps1
.\scripts\benchmark.ps1
.\scripts\benchmark-p2.ps1
.\scripts\benchmark-p3.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```

也可以直接运行：

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"
uv run python -m easypicture
uv run python -m easypicture --smoke-test
uv run python -m easypicture --io-smoke-test .cache\io-smoke
```

总体技术评估和开发路线见
[`docs/development_plan.md`](docs/development_plan.md)，当前阶段结果见
[`docs/p3_acceptance.md`](docs/p3_acceptance.md)。
