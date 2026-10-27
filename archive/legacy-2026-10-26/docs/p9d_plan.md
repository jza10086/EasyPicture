# P9D 决策完整计划：静态 SVG 基础编辑与兼容性增强

- **计划状态**：决策已冻结，等待实现
- **基线日期**：2026-08-02
- **代码基线**：`main` / `origin/main`，`9a0ff52`，EasyPicture `0.1.3`
- **前置阶段**：P9B 静态 SVG、P9C 两级图层组、预发布 schema 收敛均已人工验收
- **持久化契约**：继续使用 `.limg` schema v1

## 1. 目标、成功标准与非目标

P9D 在现有“图形”工具中增加“栅格 / 矢量”输出模式，使矩形、圆角矩形、圆、椭圆、直线、折线和多边形可以创建为可编辑的 `SvgElementObject`。导入的同类静态 SVG 在结构与样式可安全解释时进入相同编辑会话；不能无损映射的内容继续作为保真静态 SVG 子图层，而不是静默近似或丢弃。

成功标准：

1. 七类基础图形可以创建、选择、再次编辑、保存、重开和普通 SVG 导出；
2. 折线和多边形支持画布顶点拖动、属性栏精确坐标、添加、删除、排序、会话撤销和文档撤销；
3. 基础 SVG 样式直接写回标准 SVG，不依赖新增 `.limg` 私有字段；
4. 样式继承、受限基础 CSS、`currentColor`、二维变换、`use`、本地渐变和复杂文字按本计划的兼容矩阵处理；
5. 第一次编辑允许标准化 XML，但编辑前后视觉和 SVG 语义等价；不承诺原始空白、属性顺序、class 组织或源码字节级往返；
6. 普通 SVG 导出保持单根、独立、中立，不包含 EasyPicture 名称、版本、UUID、图层树或应用元数据；
7. P9D 不改变 `.limg` schema 版本和 manifest 字段集合。

P9D 明确不实现贝塞尔节点、布尔/复合路径、渐变可视化编辑、遮罩、滤镜、动画、脚本、完整 CSS 级联和浏览器级文字排版。这些项目登记在 [`docs/low_priority_issues.md`](low_priority_issues.md)。

## 2. 当前代码事实与必须保留的能力

本计划基于当前源码，而不是预设的新架构：

| 现有位置 | 当前事实 | P9D 处理 |
| --- | --- | --- |
| `domain/vector.py` / `SvgElementObject` | SVG 字符串、缓存边界、对象级变换、三项旧样式 override 是现有持久化模型 | 保持 schema v1；不增加几何字段 |
| `persistence/svg_io.py` / `import_svg()` | 已有限额、安全清理、叶图元拆分、Qt 验证和警告汇总 | 收窄为导入编排，复用统一安全、级联和引用模块 |
| `render/svg_renderer.py` | 画布、缩略图、合成和导出共用静态 `QSvgRenderer` | 保留为权威渲染和边界测量，不再承担样式改写 |
| `persistence/svg_export.py` | 已支持保留/栅格化矢量、内嵌栅格组和原子写入 | 改用统一引用重写器，继续做独立性与实际渲染验证 |
| `imaging/shapes.py` | 已有直线、矩形、圆角矩形、椭圆、多边形的栅格几何和设置 | 提取共享几何；栅格限制与 SVG 限制分开 |
| `tools/shape_tool.py` | 已有会话、画布手柄、轻量/精确预览、会话撤销路由和单命令提交 | 扩展为共享交互状态机，提交交给栅格/新建 SVG/编辑 SVG 三个 backend |
| `ui/shape_properties.py` | 多边形已有顶点选择和精确 X/Y | 增加折线、增删排序、矢量样式和一致的历史分组 |
| `commands/service.py` | 有通用矢量插入与属性命令，但 SVG 属性命令未计存储字节 | 新增专用内容替换命令并纳入撤销字节预算 |
| `persistence/project_io.py` | SVG 资源以 zlib 条目、长度和 SHA-256 保存 | 继续原格式；统一保存前和加载时安全验证 |

### 2.1 P9D-0 必须先封口的当前缺陷

以下问题会让后续测试给出假阳性、产生不可重开的工程或破坏普通 SVG 契约，必须在功能开发前修复：

1. `persistence/schema.py` 已是 schema v1，但 `app.py` 的 SVG smoke 诊断和对应 CLI 测试仍硬编码 schema v4；改为读取并断言 `PROJECT_SCHEMA_VERSION` 和实际 manifest。
2. 导入器允许经过验证的 `data:image/png`，`.limg` 加载器却无条件禁止 `<image>`；统一安全策略，使“导入 → 保存 → 重开”结果一致。
3. `.limg` 中 `<style>` 的外部 `url(...)`、`@import` 等必须与普通导入执行同一 token 级安全检查，不能只扫描 XML 属性。
4. `svg_io._normalize_clip_paths()` 生成 `easypicture-mask-*`；改为确定性的中立短 ID，并把中立性断言改为大小写不敏感。
5. 当前 ID 重写漏掉带引号/空白的 `url()`、CSS `#id` 选择器、重复源 ID和部分 `href`；改为 XML/CSS token 级统一重写。
6. `layer_panel.py` 对 `SvgElementObject` 跨组拖放仍调用文字对象查询；改用通用矢量对象 API并补 UI 回归。
7. `_VectorObjectPropertyCommand.storage_bytes` 当前为 0；所有包含 SVG before/after 的命令必须按 UTF-8 实际字节计入现有撤销预算。

## 3. 冻结的产品与交互决策

### 3.1 工具入口与输出目标

- 不新增独立矢量工具；现有“图形”工具增加持久化的“输出：栅格 / 矢量”选项。
- 默认保持“栅格”，避免升级后改变既有操作结果。
- 矢量模式支持七类：矩形、圆角矩形、圆、椭圆、直线、折线、多边形。
- 圆是独立类型；即使宽高数值相等，也不在圆和椭圆之间自动改类型。
- 矢量模式不显示/不接受栅格专有的抗锯齿和混合模式；对象级混合仍保持当前支持边界，不能静默忽略控件值。
- 新 SVG 对象的插入目标依次为：当前选中的矢量组；当前矢量子层的相邻位置；最上方未锁定矢量组；均不存在时创建“矢量组”并插入对象。创建组和对象是同一条文档撤销命令。
- 选择可编辑的基础 SVG 子层后激活同一“图形”工具，即进入现有对象编辑会话；`path`、`use` 和兼容只读对象仍可做对象级移动、缩放、不透明度、排序和栅格化，但不显示伪造的几何编辑能力。

### 3.2 创建手势

- 矩形、圆角矩形、椭圆：按下点和当前点定义包围框。
- 圆：按下点为方框一角，拖动时取两个轴绝对位移的较小值形成正方形；拖动方向保持。
- 直线：按下点和抬起点为两个端点。
- 折线、多边形：单击追加顶点，`Backspace` 删除草稿末点，双击或 `Enter` 完成，`Esc` 取消；折线至少 2 点，多边形至少 3 点。
- 新建会话中不修改文档；只有“应用”或 `Enter` 完成后才产生一条文档命令。

### 3.3 顶点编辑

- 画布显示每个顶点手柄，拖动只修改当前顶点；属性栏顶点下拉框与画布选中状态双向同步。
- 顶点 X/Y 显示文档/画布坐标。内部 SVG 变换和对象级变换均可逆时，通过逆 CTM 写回叶元素本地坐标；不可逆矩阵进入兼容只读。
- “添加”插在当前顶点之后：多边形取当前点到下一点（末点回到首点）的中点；折线非末点取下一段中点，末点按最后一段方向外推半段。
- “删除”在折线只剩 2 点、多边形只剩 3 点时禁用。
- “上移/下移”与相邻点交换，不循环跨越首尾；选中状态跟随被移动的顶点。
- SVG 多边形允许自交，由 `fill-rule` 决定填充；现有栅格多边形继续保留不自交限制。
- 可编辑顶点上限为 4096。超过上限的导入对象完整保留和渲染，但几何只读。

### 3.4 会话撤销与文档撤销

- 会话历史项包含几何和全部 P9D 基础样式，不能只记录 geometry。
- 每次画布拖动从按下到抬起、每次增删排序、每次离散控件修改是一条会话历史；数值框从 interaction-start 到 interaction-finish 合并为一条。
- 新操作发生在撤销后时截断 redo 分支。
- `Ctrl+Z/Ctrl+Y` 在活跃编辑会话中只操作会话栈；到达会话边界时不穿透到文档栈。
- 会话历史同时限制为 100 步和 64 MiB，按估算字节淘汰最旧可撤销项；取消会话始终丢弃整个临时模型并恢复原文档。
- “应用”把最终结果提交为一条文档命令；文档 undo/redo 必须逐字节恢复 before/after SVG、边界、类型和旧 override 状态。

## 4. 临时模型与模块边界

### 4.1 共享 Qt-free 几何

新增 `src/easypicture/domain/shape.py`，承载不可变、Qt-free 的共享类型：

- `ShapeKind`: `RECTANGLE`、`ROUNDED_RECTANGLE`、`CIRCLE`、`ELLIPSE`、`LINE`、`POLYLINE`、`POLYGON`；
- `ShapeGeometry`: 直接几何字段和 points；圆角矩形保留独立 `rx/ry`；
- 共同有限数、尺寸和最小点数校验。

`imaging/shapes.py` 保留栅格设置、简单多边形限制和 Pillow 栅格化；先通过兼容 re-export 迁移现有调用，避免一次性重写图形工具全部测试。

### 4.2 SVG 临时编辑包

新增 `src/easypicture/svg/`：

| 模块 | 责任 |
| --- | --- |
| `model.py` | `SvgShapeStyle`、`NoPaint/SolidPaint/LocalPaintReference`、可编辑性和结构化诊断 |
| `values.py` | number、length/percentage、points、颜色、transform-list、viewBox/CTM 解析与标准化 |
| `cascade.py` | 受限 author CSS、presentation attributes、inline style、继承、specificity、source order、`!important`、`currentColor` |
| `references.py` | 本地引用图、循环检测、资源闭包、XML/CSS token 级 ID 重写 |
| `codec.py` | `SvgElementObject.svg` 与临时强类型图形之间解码/写回；返回可编辑或兼容只读原因 |
| `safety.py` | 普通导入、`.limg` 加载和导出复验共用的静态安全规则及资源上限 |

编辑会话持有 `EditableSvgShape(geometry, style, target, local_to_object, diagnostics)`；这些字段绝不写入 `.limg`。一个可编辑 SVG 子层必须能在 defs 外定位到唯一目标基础图元；定位来自当前独立 fragment 的结构，不新增 UUID 或工程专有 locator。

### 4.3 三个提交 backend

`ShapeTool` 保持一个交互状态机，按目标委托：

1. `RasterShapeTarget`：继续调用现有栅格计算和 `apply_pixel_tiles()`；
2. `NewSvgShapeTarget`：生成中立 standalone SVG，经 Qt 复验后原子插入；
3. `ExistingSvgShapeTarget`：解码当前 SVG，会话结束后原子替换内容。

矢量模式的鼠标移动只绘制轻量 QPainter overlay；鼠标抬起、数值交互结束或结构变化后才刷新精确临时 SVG。无矢量组时使用虚拟预览目标，预览不得为了显示而提前创建文档节点。

## 5. 标准 SVG 写回契约

### 5.1 几何属性

写回以下标准属性，不创建 EasyPicture 字段：

| 类型 | 标准属性 |
| --- | --- |
| 矩形 | `rect x y width height` |
| 圆角矩形 | `rect x y width height rx ry` |
| 圆 | `circle cx cy r` |
| 椭圆 | `ellipse cx cy rx ry` |
| 直线 | `line x1 y1 x2 y2` |
| 折线 | `polyline points` |
| 多边形 | `polygon points` |

新对象使用稳定的十进制格式和可读 presentation attributes；不写对象 UUID。导入对象只最小修改目标图元的几何/样式并保留安全 defs、未知但安全的属性和内部二维 transform。第一次编辑可以物化继承和 CSS，不承诺保留原选择器组织。

水平/垂直线、透明对象或 fill/stroke 均为 `none` 时仍必须有正的选择边界：渲染边界优先，退化时使用几何 AABB 加最小 0.5 user-unit 选择 padding。视觉输出不因选择 padding 增加隐形图元。

### 5.2 样式模型

P9D 支持：

- `fill`: `none`、sRGB 纯色、本地 paint server 引用；
- `stroke`: `none`、sRGB 纯色、本地 paint server 引用；
- `stroke-width >= 0`；
- `stroke-linecap`: `butt | round | square`；
- `stroke-linejoin`: `miter | round | bevel`；
- `stroke-miterlimit >= 1`；
- `stroke-dasharray`: `none` 或最多 64 个非负有限数，不能全部为 0；
- `fill-rule`: `nonzero | evenodd`；
- 目标图元 `opacity`、`fill-opacity`、`stroke-opacity`。

颜色 alpha 以 `fill/stroke + fill-opacity/stroke-opacity` 写出，不依赖八位 hex。对象级 `SvgElementObject.opacity` 在 UI 标为“图层不透明度”，目标元素 `opacity` 标为“图形不透明度”，两者乘积语义不混合。

直线和折线固定无填充并禁用填充控件。fill/stroke 同时为 `none` 合法，仍可从图层树和手柄再次选中。

本地渐变保持为 `LocalPaintReference`：几何可编辑，渐变 paint 只读；用户可显式替换成纯色或无填充/无描边。P9D 不编辑 stop、坐标或 `gradientTransform`。

### 5.3 旧 override 的兼容迁移

schema v1 的 `fill_override`、`stroke_override`、`stroke_width_override` 继续读取和写回，保证未编辑旧工程往返。

第一次 P9D 几何或样式提交时：

1. 把三个旧 override 的实际结果物化到目标 SVG；
2. 应用本次编辑；
3. 原子清空三个 override；
4. 同时重算 `element_kind` 和 `intrinsic_bounds`；
5. before/after 全部进入同一文档命令。

新建 P9D SVG 对象始终把样式写进 SVG，三个 override 为 `None`。旧 `svg_with_overrides()` 仅保留为打开未编辑 schema v1 对象的兼容路径，不能承载新功能。

## 6. CSS、颜色、变换与长度的兼容范围

### 6.1 CSS 决策

P9D 引入 `tinycss2>=1.5,<2` 与 `cssselect2>=0.9,<1`，由 `uv` 锁定。禁止用分号和正则手写一个伪完整 CSS parser。

支持的 author CSS：

- presentation attributes、元素 `style`、内嵌 `<style>`；
- `*`、type、`#id`、`.class` 及其组合；descendant、child、逗号列表；专门支持静态 `:root`；
- specificity、source order、继承和 `!important`；
- P9D 绘制属性，以及 `color`、`display`、`visibility`、基础字体属性、`text-anchor`、`stop-color`、`stop-opacity`。

不支持并不猜测：动态/结构伪类、兄弟选择器、属性选择器、CSS variables、`calc()`、`@import`、`@font-face`、`@media`、动画、外部 stylesheet 和完整 user-agent/user-origin 级联。影响目标属性时，对象或该属性进入兼容只读并给出一次汇总警告。

导入拆层前在完整、已清理的树上计算受支持的 computed style，再构造每个叶的最小祖先链和本地引用闭包，避免拆掉兄弟后改变选择器语义。

`currentColor` 按继承后的 `color` 解析；第一次编辑将支持的 computed value 物化为目标元素的标准 inline style。若获胜声明为 `!important`，写回声明同样使用 author inline `!important`，确保编辑结果不被原样式表反压。无法解析的颜色不回退白色；保留静态结果并将相关属性设为只读。

### 6.2 颜色和 paint

直接编辑支持 CSS/SVG 常用命名色、3/4/6/8 位 hex、`rgb()/rgba()/hsl()/hsla()`，统一转换到 sRGB + 独立 alpha。`lab()`、`lch()`、`color()`、CSS 变量和带 fallback 的复杂 paint 只保留，不宣称可编辑。

本地 `linearGradient/radialGradient`、stop、`href` 链、`gradientUnits`、`gradientTransform` 和 `spreadMethod` 进入引用闭包；缺失、循环或外部引用产生诊断。渐变 stop 的 `currentColor` 无法可靠物化时只读保留。

### 6.3 transform、viewBox 与长度

- 支持 SVG attribute 中的 `matrix/translate/scale/rotate/skewX/skewY`，按标准顺序累积到二维 `AffineTransform`；
- root/nested `viewBox` 和 `preserveAspectRatio` 纳入 CTM；嵌套 viewport 的裁切语义必须保留；
- 几何编辑要求累计矩阵可逆；CSS transform、3D、`transform-box/origin` 或不可逆矩阵保持静态兼容只读；
- 对象级 `SvgElementObject.transform` 继续独立保存，不破坏性烘焙进所有点；
- unitless/px、96 dpi 绝对单位和百分比按具体属性解析；x/width 相对 viewport 宽，y/height 相对高，其他长度按标准归一化对角线；
- `em/ex/ch/rem/calc()/var()` 不能安全固定时只读保留。

实现必须用自有值解析器形成可测试 CTM；Qt 的 `boundsOnElement()` 与 `transformForElement()` 只用于边界和交叉验证，不作为隐藏的业务解析器。

## 7. `use`、复杂文字和不支持结构的安全降级

### 7.1 `use`

本地 `<use>` 继续静态保留、渲染和导出，但 P9D 不自动展开 shadow tree，也不把它伪装成 rect/circle。允许对象级变换、不透明度、排序和栅格化。显式“分离 use”延后到后续阶段。

引用图必须检测缺失、循环和最大 32 层深度；只允许本地 fragment 引用。导出时同时正确处理 SVG 2 `href` 与兼容查看器需要的 `xlink:href`，生成 ID 为中立、短、确定性值。

### 7.2 文字

只有可以证明无损映射的简单 `<text>` 才自动转成 `TextObject`：单一文本段、标量定位、统一受支持字体/颜色、无 `tspan/textPath`、逐字 `x/y/dx/dy/rotate`、`textLength`、writing mode 或其他分段排版。

其余复杂文字保留为 `SvgElementObject(element_kind="text")`，继续由 Qt 静态渲染和普通 SVG 导出，几何/文字内容标记为兼容只读并汇总警告。不得通过拼接 `itertext()` 静默丢失空白、分段样式或定位。显式有损“转换为可编辑文字”不在 P9D。

### 7.3 其他结构

- `path`、复杂组合结构：保真静态 SVG 子层，不提供节点编辑；
- mask/filter/animation/script/external resource：按安全边界拒绝或跳过并警告；
- 不支持 CSS/transform 仅影响某个 paint 时，允许几何编辑但该 paint 只读；影响坐标或目标定位时，整个几何只读；
- 任何降级都必须结构化记录“对象/属性、原因、采取的行为”，导入结束只显示一次汇总，详细信息写日志。

## 8. 安全、资源上限与导入流水线

统一流水线：

`读取限额 → 禁止 DTD/实体 → XML 结构限额 → 静态安全清理 → CSS token 安全扫描 → computed style → 本地引用图 → 叶分类/拆层 → Qt 静态验证 → DocumentCommandService 提交`

冻结上限：

- 源文件 32 MiB、单 fragment 16 MiB、最多 256 个导入叶节点（沿用现值）；
- XML 最多 100,000 个元素、最大深度 128、单属性值最大 256 KiB；
- 内嵌 style 总计 1 MiB、最多 10,000 条规则；
- 本地引用深度 32；
- 4096 顶点是可编辑上限，不是静态保真导入上限。

结构/安全上限超出时拒绝整个源文件，不留下半导入文档；单对象只是超出编辑能力时完整保留为兼容只读。安全内嵌 PNG 只接受已验证的 `data:image/png`，复用图像尺寸、解码后字节和可见性检查，并确保保存后加载执行同一策略。

当前每个叶节点 `deepcopy(root)` 的最坏复杂度应移除：完成一次清理和级联后，只复制必要祖先链、目标叶和其传递本地资源闭包。

## 9. 文档命令、持久化与普通导出

### 9.1 命令 API

新增窄接口，不把任意 SVG 字符串开放给通用属性更新：

- `DocumentCommandService.insert_svg_shape(...)`；
- `DocumentCommandService.replace_svg_object_content(...)`；
- `Document._replace_svg_object_content(...)` 仅接受 codec 已验证的结果。

替换命令原子持有 before/after：`svg`、`element_kind`、`intrinsic_bounds`、三个旧 override，以及实际需要变化的对象级字段。命令的 `storage_bytes` 至少包含 before/after UTF-8 SVG 和结构化负载估算，服从现有 100 条、1 GiB 文档撤销策略和超预算确认流程。

### 9.2 schema v1

- `PROJECT_SCHEMA_VERSION` 保持 1；
- manifest 不增加 geometry/style/editability/diagnostic 字段；
- SVG 仍存入现有 `vectors/{id}.svg.z`，长度、SHA-256、边界和对象级变换仍按现格式；
- 增加一个由 `0.1.3` 生成的小型 schema v1 `.limg` fixture，断言 key 集合、schema、加载/保存/重开和未编辑 SVG 往返；
- P9D 代码不得创建 v2 migration 占位。

### 9.3 中立 SVG 导出

- 输出单根标准 SVG，所有应用生成 ID 改写为 `d1/g1/c1/...` 一类中立、确定性短值；
- CSS selector、quoted/unquoted `url()`、paint fallback、`href/xlink:href` 和重复 ID 共用 token-aware 重写器；
- 不输出 EasyPicture 名称/版本、对象 UUID、图层组、工程路径或自定义 namespace；
- 保留矢量和栅格化矢量两条路径都要在移动文件后独立打开；
- 导出临时文件必须重新 XML 安全验证、`QSvgRenderer.isValid()`、实际渲染和尺寸检查后再原子替换目标。

## 10. 测试、golden 与回归矩阵

### 10.1 测试文件和 fixture 布局

建议新增：

- `tests/unit/test_p9d_svg_geometry.py`
- `tests/unit/test_p9d_svg_cascade.py`
- `tests/unit/test_p9d_svg_import.py`
- `tests/ui/test_p9d_vector_shape_ui.py`
- `tests/fixtures/svg/p9d/`
- `tests/fixtures/projects/p9d_schema_v1.limg`

并扩展 `test_p9b_svg.py`、project I/O、layer panel、canvas/render 和 CLI smoke 测试。

每个 SVG fixture 配一份期望能力/诊断 JSON：哪些几何可编辑、哪些 paint 只读、哪些结构保留、预期警告代码。不要只断言“导入数量”。

### 10.2 必测矩阵

| 类别 | 最小覆盖 |
| --- | --- |
| 七类几何 | 创建、拖动、精确数值、边界、保存重开、保留矢量导出 |
| 圆角/点集 | 独立 rx/ry、2/3 点下限、4096 上限、自交 polygon、fill-rule |
| 顶点交互 | 选择同步、添加位置、删除、上下移动、拖动合并、undo branch 截断 |
| 历史 | 会话撤销不穿透、样式可撤销、100 步/64 MiB；一次应用一条文档命令；SVG 字节预算 |
| 样式 | none/solid/渐变引用、width 0、linecap/join/miter/dash、三层 opacity、alpha 不重复 |
| 级联 | presentation、祖先继承、inline、type/id/class/descendant/child、source order、specificity、`!important`、`currentColor` |
| 变换 | 每种二维函数、组合顺序、viewBox/preserveAspectRatio、百分比、不可逆矩阵降级 |
| 引用 | `use`、渐变 href 链、引用缺失/循环、带引号 URL、CSS selector ID、clipPath 资源 |
| 文字 | 简单文本无损转换；tspan/textPath/逐字定位/复杂 CSS 保留为静态 SVG |
| 安全 | DTD/实体、脚本/事件、外部 CSS URL、外链 image、filter/mask/animation、结构限额 |
| 持久化 | 当前 schema 常量、manifest key、内嵌 PNG 往返、未编辑字节往返、编辑后标准属性 |
| 中立导出 | 大小写不敏感检查应用名；无 UUID/层树；中立 ID；文件移动后可打开 |
| 图层树 | SVG 选择、复制、删除、跨矢量组拖放、锁定目标、自动新建矢量组 |

### 10.3 golden 策略

使用三层 oracle，避免把 Qt 字体或平台差异误当协议：

1. **语义 golden**：解析输出 XML，比较元素、标准属性、引用闭包、computed style 和 diagnostics；不比较空白、属性顺序和前缀。
2. **渲染 golden**：基础纯色几何比较固定尺寸 RGBA/alpha；源 SVG → 导入 → 未编辑导出比较 Qt 实际像素。几何 alpha mask 必须精确，颜色允许每通道 1 的舍入差；差异像素比例不得超过 0.1%。
3. **兼容 smoke**：渐变、`use` 和复杂文字断言 Qt 有效、非空、边界稳定和引用完整；字体相关内容不做跨机器逐像素 golden，人工验收再用浏览器/常用查看器确认。

golden 只能通过显式更新脚本生成，更新必须同时展示 before/after 差异和 fixture 原因；测试运行不得自动改写基准。

### 10.4 性能门槛

- 1024 顶点拖动的轻量 overlay：输入到显示 P95 小于 33 ms；
- 鼠标抬起后的精确 SVG 刷新目标小于 100 ms；
- 256 叶、32 MiB 限额内导入不得呈现“每叶复制整树”的线性倍增内存；
- 性能数值记录为固定机器 benchmark，不用宽松 CI 计时断言制造偶发失败。

## 11. 分阶段实施与退出门禁

### P9D-0：基线真实化与 ADR 冻结

- 修复第 2.1 节七项前置缺陷；
- 增加当前 schema v1 fixture、可见内嵌 PNG 往返、大小写中立 ID、CSS 外链和 SVG 跨组拖放测试；
- 新增 ADR，冻结“SVG 字符串唯一真相、语义往返、受限 CSS、`use`/渐变/文字降级”决策。

**退出门禁**：现有 P9B/P9C 回归全过；SVG smoke 报告实际 schema v1；fixture 保存后都能重开；普通 SVG 无应用生成身份。

### P9D-1：共享几何、值解析和 SVG codec

- 提取共享 `ShapeGeometry`，增加 circle/polyline、独立 rx/ry；
- 实现 number/points/length/transform/viewBox 解析；
- 实现七类叶图元 decode/encode、editability 和结构化 diagnostics；
- 保留原结构并完成 Qt 复验与边界回写。

**退出门禁**：七类纯属性 SVG 双向语义测试、变换逆映射、退化边界和不可编辑原因全部通过。

### P9D-2：导入、安全、CSS 和引用兼容

- 引入并锁定 tinycss2/cssselect2；
- 统一 safety、computed style、`currentColor` 和本地引用闭包；
- 改造拆层以避免整树重复复制；
- 实现渐变几何可编辑/paint 只读、`use` 只读、复杂文字静态保留。

**退出门禁**：CSS/transform/use/gradient/text/security fixture 符合兼容矩阵；没有静默白色回退或复杂文字数据损失。

### P9D-3：文档命令、预览与撤销预算

- 实现专用 insert/replace API；
- SVG before/after 纳入字节预算；
- 扩展会话历史为 geometry + style、100 步/64 MiB；
- 增加不修改文档的虚拟矢量预览。

**退出门禁**：取消无痕、一次应用一条命令、逐字节 undo/redo、历史边界不穿透、超预算行为可预测。

### P9D-4：矢量创建模式

- 在现有图形工具加入输出模式；
- 实现七类新建手势、目标矢量组规则和原子建组；
- 栅格模式行为及现有 P6C 测试保持不变。

**退出门禁**：七类对象均可创建、保存、重开；默认仍是栅格；栅格专有控件不被静默带入矢量结果。

### P9D-5：现有图形与顶点编辑

- 可编辑 SVG 进入同一 ShapeTool 会话；
- 实现画布顶点拖动、属性栏同步、精确 X/Y、增删排序；
- 应用时最小写回几何并重算边界。

**退出门禁**：折线/多边形完整交互矩阵、内部变换逆映射、会话和文档双层撤销全部通过。

### P9D-6：基础样式编辑与 override 收敛

- 实现填充/描边/none、宽度、linecap/join/miter/dash、fill-rule 和三层 opacity UI；
- 支持 computed style 显示和标准 inline 写回；
- 首次编辑原子物化并清空旧 override。

**退出门禁**：presentation/inline/祖先/CSS `!important` 均以 Qt 实际像素验证修改生效；alpha 不重复；旧 schema v1 对象可无损撤销。

### P9D-7：导出、性能、打包与人工验收

- 完成中立 reference rewriter 和 semantic/render goldens；
- 运行全量 lint、mypy、pytest、源码 smoke、standalone 构建和隔离 PATH smoke；
- 记录 1024/4096 顶点、256 叶导入 benchmark；
- 更新开发计划、ADR、验收报告和低优先级清单。

**退出门禁**：自动门禁全部通过；人工验收覆盖七类创建/编辑、CSS/currentColor/transform/use/gradient/复杂文字、保存重开和浏览器普通 SVG；工作树只包含计划内文件。

## 12. 最终人工验收清单

1. 在同一图形工具切换栅格/矢量，确认旧栅格流程无变化；
2. 新建并再次编辑七类 SVG 图形，逐个保存、关闭、重开；
3. 对折线和多边形完成拖点、精确 X/Y、添加、删除、排序、会话 undo/redo；
4. 应用后执行文档 undo/redo，确认 SVG、边界、图层缩略图和选择手柄一致；
5. 验证 fill/stroke none、宽度 0、linecap/join/miter/dash、fill-rule 和透明度；
6. 导入包含祖先样式、inline style、基础 `<style>`、`!important` 和 `currentColor` 的 fixture；
7. 导入组合 transform/viewBox、`use`、本地渐变和引用链，检查编辑能力提示与视觉；
8. 导入 tspan/textPath/逐字定位文字，确认完整静态保留而非错误转成应用文字；
9. 导入可见内嵌 PNG SVG，保存 `.limg` 后重开；
10. 在两个矢量组之间拖放、复制、删除和撤销 SVG 子层；
11. 以“保留矢量”和“栅格化矢量”导出，移动文件后在 Qt、浏览器和常用查看器打开；
12. 检查普通 SVG 源码：无 EasyPicture 身份、UUID、层树、自定义元数据或外部资源。

## 13. 标准依据

- [SVG 2 Styling](https://www.w3.org/TR/SVG/styling.html)
- [SVG 2 Painting](https://www.w3.org/TR/SVG/painting.html)
- [SVG 2 Structure and `use`](https://www.w3.org/TR/SVG/struct.html)
- [SVG 2 Coordinate Systems, Transformations and Units](https://www.w3.org/TR/SVG2/coords.html)
- [SVG 2 Text](https://www.w3.org/TR/SVG2/text.html)
- [Qt `QSvgRenderer`](https://doc.qt.io/qt-6/qsvgrenderer.html)
- [Qt SVG options](https://doc.qt.io/qt-6/qtsvg.html)
- [tinycss2 documentation](https://doc.courtbouillon.org/tinycss2/stable/)
- [cssselect2 API reference](https://doc.courtbouillon.org/cssselect2/stable/api_reference.html)
