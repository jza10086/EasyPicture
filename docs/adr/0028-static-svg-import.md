# ADR-0028：静态 SVG 导入与安全边界

## 状态

Accepted for P9B；其中 schema v4 与迁移部分已被 ADR-0030 取代。

## 决策

EasyPicture 只导入静态 SVG。导入器先执行安全预扫描，再解析画布尺寸、`viewBox`、局部定义和可绘制叶节点。每个受支持的叶图元成为一个矢量子层；SVG `text` 转换为现有 `TextObject`，其他图元保存为独立、完整、经过清理的 `SvgElementObject` SVG 文档。

第一版支持 `path`、`rect`、`circle`、`ellipse`、`line`、`polyline`、`polygon`、`text`、本地 `use` 和安全的内嵌 PNG `image`。本地渐变及定义随独立图元保留；基础 `clipPath` 转换为 Qt SVG 可稳定处理的遮罩。标准嵌套 `svg` 视口会在导入前转换为等价分组；完全透明的内嵌 PNG 占位会被跳过。导入后的文档同时保留一个透明栅格组，便于继续使用像素工具。

## 安全与兼容规则

- 拒绝 DTD 和实体声明，不解析网络或文件系统外部资源。
- 跳过脚本、事件处理器、动画、滤镜、外部图片、外部 CSS 和未知可执行内容。
- 不支持的数据不阻止其余安全图元导入；所有跳过项汇总为一次用户警告并写入日志。
- SVG 源文件最大 32 MiB，单个持久化 SVG 片段最大 16 MiB，画布仍受 8192×8192 上限约束。
- `QSvgRenderer` 始终启用 `DisableAnimations`，渲染缓存不写回领域对象。

## 持久化

`.limg` schema v4 在矢量子层中区分 `svg_text` 和 `svg_element`。SVG 图元正文写入 `vectors/{uuid}.svg.z`，使用 zlib level 1 和 ZIP `STORE`，清单记录原始长度与 SHA-256。加载时检查路径、重复条目、解压边界、尾随数据、压缩比、长度、校验和及静态安全规则。v1/v2/v3 按既有迁移链升级，v3 文字子层迁移为 `svg_text`。

## 后果

导入优先保证应用稳定与用户可继续编辑，不追求浏览器级 SVG 完整兼容。被跳过效果可能改变视觉结果，因此警告必须明确列出类别和数量；原始文件从不被覆盖。
