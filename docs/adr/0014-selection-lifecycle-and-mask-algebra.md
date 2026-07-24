# ADR-0014：选区生命周期与蒙版数学

- 状态：Accepted
- 日期：2026-07-24

## 决策

选区继续使用文档空间中的稀疏 `uint8` 单通道 Tile，Tile 大小固定为 256。缺失 Tile
表示 0；没有任何非零 Tile 时表示“无活动选区”，像素编辑不受约束。选区变更属于项目
内容修改，会递增 Document revision、进入 modified、触发恢复草稿并保存到 `.limg` v1。

选区组合采用模糊集合语义。设当前蒙版为 A、候选蒙版为 B：替换为 B，添加为
`max(A, B)`，减去为 `min(A, 255-B)`，相交为 `min(A, B)`。没有活动选区时，组合运算
中的 A 按全零处理。结果中的全零 Tile 必须删除；全零结果恢复为无活动选区。

所有选区修改通过 `DocumentCommandService.apply_selection_tiles()` 提交，并与图层、像素
命令共用一个 `QUndoStack`、100 条命令上限和 1 GiB PatchStore 预算。选区补丁保存完整
Tile 的 before/after 数据，使用现有 zlib level 1 或原始字节策略。无实际变化不得创建
命令、revision 或 modified。

## 原因

`.limg` v1 已经保存 selection Tile。让选区成为正式项目内容，可使保存、自动恢复、
撤销 clean index 和关闭提示保持一致。统一灰度数学避免矩形、魔棒和后续工具各自解释
半透明选区。

## 后果

- 全选会创建覆盖画布的显式 255 Tile，反选只对活动选区启用；
- 取消选择及减去到空集会恢复“无选区即无约束”的编辑语义；
- P5 不改变 `.limg` schema，也不保存蚂蚁线、临时矩形或运行时缓存。
