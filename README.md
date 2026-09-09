# EasyPicture

EasyPicture 是一款面向 Windows 的本地离线轻量图像编辑器，适合个人日常图片处理。项目以 Python 3.14、PySide6、NumPy 和 Pillow 为基础，权威像素采用 256 px Tile 存储，画布提供 OpenGL 3.3 与软件双渲染后端。

当前版本支持两级栅格/矢量图层组、画笔与常用像素工具、统一选区、自由变换、基础图形、可编辑文字、静态 SVG、非破坏性颜色调整、像素精确工作区、手动精灵帧切分、多工程标签、独立帧编辑、精灵动画与洋葱皮、Tile 无缝检查与过渡帧生成、Atlas 导出、`.limg` 工程保存和七种常用图片格式导出。EasyPicture 目前仅面向本机单用户使用，不以跨平台发布、多人协作或超大画布为主要目标。

## 版本记录

### 0.7.0（待人工验收，尚未发布）— 2026-08-31

- P10E：从当前栅格层提取调色板与自动减色，支持 1～256 色、Lab 映射、可选抖动和本机命名预设。
- 增加内/外像素描边、4/8 邻域和 Alpha 阈值，应用为同组独立栅格层，来源像素不变。
- 复用完整原子预览、最新请求调度和一次撤销；支持源图与独立帧，项目格式保持 `.limg schema v4`。
- 新增“AI 像素整理”：OKLab 加权自动配色、锁色/取色、手动像素网格、透明边缘处理及三种抖动；可输出新结果层或低分辨率新工程，来源像素不变。[使用与验收](docs/ai_pixel_cleanup_acceptance.md)。
- 验收步骤见 [P10E 验收报告](docs/p10e_acceptance.md)。正式版本号与提交待人工验收后更新。

### 0.6.0 — 2026-08-30

P10D 已通过人工验收。

- 增加每工程唯一的只读 Tile 检查标签，可将现有 linked/detached 精灵帧作为 Tile A/B 输入。
- 支持带整数循环偏移的 3×3/5×5 平铺、四方向相邻对比、RGBA 接缝统计和固定屏幕宽度热力图；属性栏只显示当前预览模式使用的参数。
- 数值与枚举属性输入支持右键“设为默认值”（恢复预设），保留编辑中右键取消与动态范围钳制。
- 支持硬切与 Bayer 4×4 有序抖动过渡；生成结果只选择来源完整 RGBA 像素，不创建中间颜色。
- 过渡结果以单命令创建为未分组独立帧，可继续使用现有像素、图层、文字和 SVG 工具手工修整；`.limg` 继续使用 schema v4。
- 大 Tile 在后台合成、分析和准备完整预览，连续改参只保留最新请求；支持取消、重复块共享和源帧缓存，刷新保留缩放和平移。

### 0.5.0 — 2026-08-27

- 将精灵帧分组升级为动画，每个动画保存独立 FPS、播放模式；每帧使用整数保持拍数控制时长。
- 增加动画只读画布标签、第一帧/逐帧/播放控制和单次、循环、往返三种调度模式。
- 增加可调前后帧动画洋葱皮；精灵切片使用保留原色的半透明参考帧，支持自动跟随最近创建帧和手动拾取。锚点改为带可调半径的红色视觉标记，不再改变动画帧的对齐位置；这些预览均不进入像素、缩略图或导出。
- `.limg` 升级为 schema v4；Atlas JSON 升级为 v2，并记录动画顺序、FPS、播放模式与帧保持拍数。

### 0.4.0 — 2026-08-19

- 增加同一窗口多工程工作区，主画布使用“工程 → 源图/独立帧”两级标签，工程间保留各自撤销历史。
- 精灵帧支持 linked 引用与 detached 独立内容；独立帧作为父工程内嵌完整文档，可继续使用图层、像素和矢量编辑能力。
- 增加帧分组、分组标签和完整 Atlas PNG + JSON 导出；`.limg` 升级为 schema v3 并保存分组与嵌入帧。
- 新建工程使用唯一的 Untitled 数字后缀；精灵帧面板支持框选、Shift/Ctrl 多选、Delete 删除、批量移动及带插入光标的稳定拖放。

### 0.3.0 — 2026-08-03

- 增加像素网格、统一单格网格、网格吸附和栅格工具整数坐标模式。
- 增加固定尺寸手动精灵切片框、逐像素/单格步进、精确 X/Y 和画布内确定按钮；切片只保存源画布区域引用，不修改原图。
- 增加底部精灵帧条、缩略图、名称、顺序、锚点和单帧 PNG 导出；源图变化后帧内容随权威可见合成更新。
- `.limg` 升级为 schema v2，正式支持精灵工作区元数据，并提供 v1→v2 迁移。

### 0.2.0 — 2026-08-03

- 完成基础 SVG 支持：矩形、圆、椭圆、直线、折线和多边形均可编辑并按标准属性序列化。
- 图形工具可选择栅格或矢量输出；折线和多边形支持逐顶点画布拖动、精确坐标、增删及排序。
- SVG 属性栏补充填充、描边、端点、连接、填充规则和虚线；导入支持安全的基础 CSS 继承，复杂文字不再被有损转换。
- SVG 继续作为中立交换格式，EasyPicture 编辑结构只保存在 `.limg schema v1`。

### 0.1.3 — 2026-08-02

- 重整文件工作流：“打开”仅用于 `.limg` 工程，“导入”把图片立即保存为新的 `.limg` 工程，“追加”把图片或其他工程作为新图层组加入当前工程。
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

- `.limg` 是唯一保存 EasyPicture 图层树、对象属性、精灵帧、动画和工程身份的格式，当前 schema 为 v4。
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

总体架构与开发路线见 [`docs/development_plan.md`](docs/development_plan.md)，当前阶段验收见 [`docs/p10d_acceptance.md`](docs/p10d_acceptance.md)，界面输入约定见 [`docs/ui_design_principles.md`](docs/ui_design_principles.md)，低优先级问题见 [`docs/low_priority_issues.md`](docs/low_priority_issues.md)。
