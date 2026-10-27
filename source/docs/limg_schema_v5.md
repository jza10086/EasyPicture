# .limg schema v5

仅接受 `schema_version: 5`。v1～v4、未知版本和旧两级组节点类型直接报错，
不迁移、不自动重写、不删除旧工程或恢复数据。历史工作区曾使用中间 v5 描述，
该两级结构不是当前格式；同样拒绝。

文件仍是 ZIP 容器，以 UTF-8 `manifest.json` 为入口。保存写临时文件，
完整成功后原子替换目标；取消、编码和替换失败不会覆盖原有有效文件。
项目不生成、计算或比较文件／资源摘要。读取验证结构、资源路径、大小、解码结果和像素布局；
回归测试直接比较节点属性、矩阵和像素数组。

## 文档

顶层包含 `format`、`schema_version`、`document_kind`、`document_id`、
`canvas`、`working_color_space`、`nodes`、`selection`、`sprite_workspace`、
`thumbnail`、`recovery`。`active_layer_id` 是可空的编辑目标提示，不是对象树权威数据。
支持空 `nodes`、纯矢量、根对象及递归混合集合。文档工作色彩空间为 sRGB。

根 `nodes` 和集合 `children` 的数组顺序为从后到前的绘制顺序，
UI 大纲按相反顺序显示。父子关系由嵌套结构唯一表达；没有独立父引用副本。
UUID 在同一文档内唯一，不允许重复引用、共享子节点或循环。
节点总数最多 100,000，深度最多 64，栅格对象最多 256。

| 节点类型 | 必需语义 |
| --- | --- |
| `collection` | UUID、名称、显隐、不透明度、锁定、有序 `children`；无矩阵 |
| `raster` | 公共对象属性、局部 `size`、整数 `origin`、`alpha_locked`、`sampling`、Tile 资源 |
| `svg_text` | 公共对象属性、文字、文本框、字体、排版、填充／描边 |
| `svg_element` | 公共对象属性、原始边界、元素种类、独立静态 SVG 资源及样式覆盖 |

公共对象属性为 `id`、`name`、`visible`、`opacity`、`locked`、
六值 `transform` 和可空二维 `transform_pivot`。矩阵不把栅格像素烘焙进画布；
局部像素可以位于输出范围之外。栅格世界矩阵是局部矩阵之后叠加 `origin` 平移。
采样方式为 `nearest` 或 `smooth`。

## 资源

栅格 Tile 为 256×256、4 通道 uint8 RGBA，资源编码 `rgba8-zlib`；
像素选区 Tile 为 256×256 单通道 uint8，编码 `mask8-zlib`。
描述含 `x`、`y`、`path`、`encoding`、`raw_size`，不含摘要。
全零 Tile 可以省略。`layers/<对象 UUID>/tiles/` 只是资源目录命名，
不表示另有旧栅格层列表。

SVG 资源位于 `vectors/<对象 UUID>.svg.z`。静态 SVG 禁止脚本、动画和外部资源引用；
复杂路径可以整体移动和变换，不提供贝塞尔节点或布尔编辑。
缩略图为派生 PNG，不作为像素权威源。

独立精灵帧以 `sprite_frames/<帧 UUID>/document.limg` 嵌入；
内部使用同一 v5 节点契约，文档类型为帧，不能再包含精灵工作区或嵌入文档。
帧引用与嵌入文档一一匹配，帧输出尺寸匹配父工程精灵单元大小。
动画、洋葱皮、Tile 和 Atlas 仍依赖这些帧引用，最终输出按帧画布合成。

## 读取和追加

拒绝不安全路径、重复 ZIP 项、重复 UUID、未知节点、断裂目标／帧引用、
非法布尔／数值／矩阵、超限尺寸、缺失资源、损坏压缩流和错误解码字节数。
清单上限 4 MiB，总解码上限 4 GiB，SVG 单资源上限 16 MiB、总计 256 MiB，
并限制容器项数量和压缩比。资源坐标必须在对应局部尺寸允许的 Tile 范围内。

追加工程为导入节点重建 UUID 和引用，保持局部像素、矩阵、嵌套关系与相对绘制顺序。
复制使用新 UUID 和 COW 像素，后续编辑相互独立。自动恢复写入相同格式；
旧恢复条目拒绝加载但保留原文件。
