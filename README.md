# EasyPicture

EasyPicture 是一个面向 Windows 的本地离线轻量位图编辑器。当前仓库已完成 P5
可实际编辑版本：包含单文档生命周期、分块像素/选区存储、多图层管理、完整帧
OpenGL/软件显示、图层与像素撤销、常用图片导入、`.limg` 项目保存/恢复、自动恢复、PNG 导出，
画笔、橡皮、吸管、油漆桶，以及矩形、魔棒和可调整的统一选区系统。

P6A 自由变换与 P6R GPU 画布重构已经完成联合人工验收：支持整层或活动选区
的移动、单边/规则缩放、旋转、不规则四角编辑、实时投影预览、四角精确坐标及原子
撤销；显示链使用 OpenGL 3.3 完整帧并提供软件回退，优先保证旋出/旋回、画笔、
显隐和导航期间不残留旧帧。自由变换预览支持按操作分步撤销、拖动右键复位，中心点
与四角坐标集中在属性面板；全局设置可调整普通及 Shift 数值拖动灵敏度。套索、网格
变形、裁切、形状、文本和完整格式导出尚未实现。

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
.\scripts\benchmark-p4.ps1
.\scripts\benchmark-p5.ps1
.\scripts\package.ps1
.\scripts\smoke-standalone.ps1
.\scripts\smoke-render-standalone.ps1
```

也可以直接运行：

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"
uv run python -m easypicture
uv run python -m easypicture --smoke-test
uv run python -m easypicture --io-smoke-test .cache\io-smoke
uv run python -m easypicture --render-backend opengl --render-smoke-test .cache\render-smoke
```

总体技术评估和开发路线见
[`docs/development_plan.md`](docs/development_plan.md)，当前阶段结果见
[`docs/p5_acceptance.md`](docs/p5_acceptance.md)。界面输入控件约定见
[`docs/ui_design_principles.md`](docs/ui_design_principles.md)，P6A 结果见
[`docs/p6a_acceptance.md`](docs/p6a_acceptance.md)，P6R 渲染重构见
[`docs/p6r_acceptance.md`](docs/p6r_acceptance.md)。
