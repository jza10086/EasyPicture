# EasyPicture

EasyPicture 是一个面向 Windows 的本地离线轻量位图编辑器。当前仓库已完成 P1
应用骨架：包含单文档生命周期、领域模型、分块像素/选区存储、可缩放平移画布、
工具切换框架、颜色与窗口设置以及 QImage 显示桥。

图层合成、撤销、文件读写和正式图像编辑工具尚未实现。

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
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
```

也可以直接运行：

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"
uv run python -m easypicture
uv run python -m easypicture --smoke-test
```

总体技术评估和开发路线见
[`docs/development_plan.md`](docs/development_plan.md)，当前阶段结果见
[`docs/p1_acceptance.md`](docs/p1_acceptance.md)。
