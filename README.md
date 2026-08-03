# EasyPicture

EasyPicture 是一款面向 Windows 的本地离线轻量图像编辑器，适合个人日常图片处理。项目以 Python 3.14、PySide6、NumPy 和 Pillow 为基础，权威像素采用 256 px Tile 存储，画布提供 OpenGL 3.3 与软件双渲染后端。

当前版本支持两级栅格/矢量图层组、画笔与常用像素工具、统一选区、自由变换、基础图形、可编辑文字、静态 SVG、非破坏性颜色调整、`.limg` 工程保存和七种常用图片格式导出。EasyPicture 目前仅面向本机单用户使用，不以跨平台发布、多人协作或超大画布为主要目标。

## 版本记录

### 0.2.0 — 2026-08-03

- 完成基础 SVG 支持：矩形、圆、椭圆、直线、折线和多边形均可编辑并按标准属性序列化。
- 图形工具可选择栅格或矢量输出；折线和多边形支持逐顶点画布拖动、精确坐标、增删及排序。
- SVG 属性栏补充填充、描边、端点、连接、填充规则和虚线；导入支持安全的基础 CSS 继承，复杂文字不再被有损转换。
- SVG 继续作为中立交换格式，EasyPicture 编辑结构只保存在 `.limg schema v1`。

### 0.1.3 — 2026-08-02

- 重整文件工作流：“打开”仅用于 `.limg` 工程，“导入”把图片初始化为新工程，“追加”把图片或其他工程作为新图层组加入当前工程。
- 追加操作保留当前工程身份，自动处理 UUID 与重名，并以一次命令完成撤销；不同画布尺寸按左上角对齐和裁切。
- 预发布兼容层已清理，`.limg` 以当前两级图层树重新建立 schema v1 基线。

### 0.1.2 — 2026-08-02

- 增加静态 SVG 导入、标准 SVG 导出和矢量图层栅格化。
- 每个受支持的 SVG 图元作为独立矢量子层；不支持或存在外部资源风险的内容会跳过并汇总警告。
- SVG 导出可嵌入栅格组，也可选择保留矢量子层或将其栅格化。

### 0.1.1 — 2026-08-01

- 增加可编辑文字矢量对象和严格两级的栅格组/矢量组图层树。
- 支持文字框、字体、字号、颜色、描边、换行、变换、缩略图及工程持久化。
- 组显隐、不透明度和锁定对子层使用穿透式继承。

### 0.1.0 — 2026-07-31

- 完成本机稳定版基础：日志、临时文件清理、用户提示、关于窗口和最近文件。
- 提供 OpenGL/软件完整帧画布、Tile 像素存储、撤销、自动恢复和 `.limg` 原子保存。
- 提供画笔、橡皮、吸管、油漆桶、图形、选区、自由变换、颜色调整及 PNG/JPEG/WebP/BMP/TIFF/TGA/静态 GIF 导出。

## 当前格式边界

- `.limg` 是唯一保存 EasyPicture 图层树、对象属性和工程身份的格式，当前 schema 为 v1。
- 普通图片和 SVG 是中立交换格式，不写入 EasyPicture 内部 UUID、图层树或应用元数据。
- SVG 仅支持静态安全子集；动画、脚本、外部资源、滤镜和任意深度子图层不在当前范围内。
- 栅格层始终与画布同尺寸且 origin 为零；裁切或缩小后，画布外像素只能通过撤销恢复。

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
.\scripts\smoke-render-standalone.ps1
.\scripts\smoke-export-standalone.ps1
```

也可以直接运行：

```powershell
$env:UV_CACHE_DIR = "$PWD\.cache\uv"
uv run python -m easypicture
uv run python -m easypicture --smoke-test
uv run python -m easypicture --io-smoke-test .cache\io-smoke
uv run python -m easypicture --render-backend opengl --render-smoke-test .cache\render-smoke
uv run python -m easypicture --svg-smoke-test .cache\svg-smoke
```

总体架构与开发路线见 [`docs/development_plan.md`](docs/development_plan.md)，当前阶段验收见 [`docs/p9d_acceptance.md`](docs/p9d_acceptance.md)，界面输入约定见 [`docs/ui_design_principles.md`](docs/ui_design_principles.md)，低优先级问题见 [`docs/low_priority_issues.md`](docs/low_priority_issues.md)。
