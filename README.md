# EasyPicture

EasyPicture 是一个面向 Windows 的本地离线轻量图像编辑器。当前仓库已实现至 P9B：
包含单文档生命周期、分块像素/选区存储、多图层管理、完整帧
OpenGL/软件显示、图层与像素撤销、常用图片导入、`.limg` 项目保存/恢复、自动恢复、
七种常用图片导出，
画笔、橡皮、吸管、油漆桶，以及矩形、魔棒和可调整的统一选区系统。

P6A 自由变换与 P6R GPU 画布重构已经完成联合人工验收：支持整层或活动选区
的移动、单边/规则缩放、旋转、不规则四角编辑、实时投影预览、四角精确坐标及原子
撤销；显示链使用 OpenGL 3.3 完整帧并提供软件回退，优先保证旋出/旋回、画笔、
显隐和导航期间不残留旧帧。自由变换预览支持按操作分步撤销、拖动右键复位，中心点
与四角坐标集中在属性面板；全局设置可调整普通及 Shift 数值拖动灵敏度。套索、网格
网格变形尚未实现。P6B 已完成自动与人工验收，包括可扩展到画布外的裁切、裁切透明像素、九点锚点画布尺寸、
图像/图层缩放和裁切到选区；所有图层固定与画布同尺寸且 origin 为零，缩小或裁切后位于
画布外的像素会被删除，只能通过撤销恢复。Delete 可按选区权重清除活动层 Alpha，画笔与
油漆桶支持正常、替换、正片叠底、滤色和叠加模式；颜色 Dock 保持紧凑，点击前景色或
背景色后使用深色中文 Blender 式 HSV 圆盘、明度条和 RGB/HSV/Hex 分页拖动输入弹窗。
图层列表为每个图层显示带透明棋盘格的像素缩略图，并按图层 revision 精确刷新。
几何预览继续使用统一完整帧状态，整次提交只产生一个撤销命令。P6C 基础图形已完成验收：
直线、矩形、圆角矩形、椭圆和多边形在确认前可通过画布手柄或右侧属性继续编辑，支持独立
描边/填充颜色、选区、五种混合模式和会话内撤销，确认后作为普通栅格像素一次写入。
P7A 颜色调整已完成验收：右侧非模态调整页提供反相、阈值、亮度/对比度、伽马、曲线、
色相/饱和度/明度、渐变映射、颜色替换和 Lab 调色板映射；全分辨率结果通过后台任务
计算并原子替换预览，应用后只形成一个像素撤销命令，并支持 Enter 应用。P7B 完整导出
已完成人工验收：统一窗口支持 PNG、JPEG、WebP、BMP、TIFF、TGA 和静态 GIF，提供格式参数、
白色透明背景处理、ICC/元数据规则、原子覆盖、进度和取消。P9A 增加文字组和可编辑
文字对象；P9C 将图层树统一为严格两级的栅格组/矢量组结构。P9B 增加静态 SVG 导入与
导出：每个受支持的 SVG 图元成为独立矢量子层，不支持或存在外部资源风险的内容会被跳过并
汇总警告；SVG 导出会嵌入栅格组，并可选择保留矢量子层或将其栅格化。矢量组/子层也可通过
图层右键菜单转换为普通栅格组。当前 `.limg` 仅支持正式发布前重置后的 schema v1，
不读取开发期 v1～v4 项目。动画 SVG、脚本、
外部资源、滤镜、富文本、任意深度子图层和嵌入字体仍未实现。
所有普通图片和 SVG 导出均作为中立交换格式，不包含 EasyPicture 内部 UUID、图层树或应用身份；
这些可编辑工程信息只保存在 `.limg`。

P8 将项目收束为本机 0.1.0 稳定版：运行日志采用 UTF-8 轮转文件，未捕获异常和后台任务
结果会写入日志；大图工作文件统一进入应用专属缓存并保守清理 24 小时前的遗留内容。
文件菜单提供最近 10 个项目或图片及失效路径处理，帮助菜单提供日志目录和“关于
EasyPicture”，其中可复制版本、依赖、渲染后端及本机数据目录诊断信息。

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

总体技术评估和开发路线见
[`docs/development_plan.md`](docs/development_plan.md)，当前阶段结果见
[`docs/p9b_acceptance.md`](docs/p9b_acceptance.md)。界面输入控件约定见
[`docs/ui_design_principles.md`](docs/ui_design_principles.md)，P6A 结果见
[`docs/p6a_acceptance.md`](docs/p6a_acceptance.md)，P6R 渲染重构见
[`docs/p6r_acceptance.md`](docs/p6r_acceptance.md)，P6B 几何工具验收见
[`docs/p6b_acceptance.md`](docs/p6b_acceptance.md)，P6C 图形工具验收见
[`docs/p6c_acceptance.md`](docs/p6c_acceptance.md)。
