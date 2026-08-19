# ADR-0034：独立帧、帧分组与 Atlas 交换格式

状态：Accepted（P10B 人工验收通过）

精灵帧包含 `linked` 与 `detached` 两种内容模式。引用帧始终读取根工程源画布的最新权威合成；独立化会捕获当时的可见区域并创建一个固定为单格尺寸的内嵌完整文档，原图不变。内嵌文档禁止精灵工作区与递归嵌入，并与父工程共享一条撤销历史和 1 GiB PatchStore 预算。

`.limg schema v3` 保存有序帧分组、内容模式和内嵌文档。内嵌文档位于 `sprite_frames/{frame_id}/document.limg`，仍使用同一受验证项目合同；v2 迁移为未分组引用帧。

Atlas 默认按非空分组逐行排列，未分组为最后一行。PNG 使用透明背景，JSON schema v1 保存尺寸、单格、分组顺序、帧 UUID、矩形、锚点与内容模式。PNG 和 JSON 验证后成对替换；导出不改变工程 revision、modified 或撤销状态。
