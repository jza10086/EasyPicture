# ADR-0017：浮动选区、重采样与复合撤销

- 状态：Accepted
- 日期：2026-07-27

## 决策

没有活动选区时变换整个活动层；存在活动选区时，按 `mask / 255` 将像素拆分为原位
基础部分和临时浮动部分。提交后选区蒙版使用同一矩阵变换，画布外蒙版被裁切。

最终结果由串行 JobRunner 从会话开始时的 `DocumentSnapshot` 计算。仿射和四角
投影均使用同一 3×3 矩阵；输出按 256 Tile
生成，每个 Tile 后检查取消。超过 64 MiB 的中间表面使用临时 memmap。最近邻直接
采样 straight RGBA；其他插值先处理预乘 RGB/Alpha，再转回 straight RGBA，并在
输出 Alpha 为零时保留独立插值的隐藏 RGB。

图层新边界取四个目标角点的 `floor/ceil` 整数包围盒；选区变换时与原图层边界取并集。
画布外像素保留，但单层宽高仍限制为 8192，origin 遵守 `.limg v1` 安全范围。

`DocumentCommandService.apply_raster_transform()` 在执行前校验 document ID、revision、
锁定状态、边界和撤销预算。图层 size/origin、像素 Tile 和选区 Tile 由一个复合命令
原子提交，只产生一次文档内容 revision 和一个撤销条目。

## 后果

- 取消和过期结果不会留下任何权威像素变化；
- undo/redo 同时恢复图层几何、像素和选区；
- 透明像素锁定层拒绝变换，避免破坏既有 Alpha 锁定约束；
- 变换不会写入撤销历史以外的临时项目状态。
