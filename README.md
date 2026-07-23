# EasyPicture

EasyPicture 是一个面向 Windows 的本地离线轻量位图编辑器。当前仓库处于 P0
架构验证阶段，只包含最小 Qt 应用、分块像素存储原型、QImage 显示桥和验证工具，
尚未实现正式图像编辑功能。

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
[`docs/development_plan.md`](docs/development_plan.md)。
