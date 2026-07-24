# ADR-0012：临时像素预览与单命令提交

- 状态：Accepted
- 日期：2026-07-24

## 决策

交互式像素工具使用 `PixelEditSession`。会话首次触及 Tile 时从权威
`TilePixelStore` 复制数据，后续 dab 只修改会话私有副本。`TileRenderer` 可以将指定
图层的临时 Tile 代入正常底到顶合成，但这些 Tile 不进入 Document、图层像素缓存、
revision、modified 或撤销栈。

预览、像素提交、图层显隐、不透明度、排序或图层列表变化使合成 Tile 失效时，
渲染器将旧合成标记为 stale 并继续显示；新帧按渐进调度生成完成后再原位替换。
只有从未生成过的 Tile 才在等待期间显示棋盘格，避免局部或整画布闪烁。

一次按下到释放仅在结束时调用一次 `DocumentCommandService.apply_pixel_tiles()`。
Esc、工具切换、文档替换、活动层变化或 document revision 失配会清除临时 Tile，
不提交部分结果。提交仍由命令服务执行锁定、透明像素锁定、无变化过滤和撤销预算
检查。

## 原因

逐事件直接修改 Document 会产生大量 revision 和撤销命令，也会让取消笔划难以恢复；
只在松开时渲染又无法提供及时反馈。临时覆盖兼顾实时显示、正确图层顺序和领域模型
事务边界。

## 后果

- 画笔与橡皮的一次手势固定对应零或一条命令；
- 上方图层仍可正确遮挡活动层预览；
- 预览 Tile 不占用持久化格式或常规源 QImage 缓存；
- 画笔轮廓由 viewport 前景层同步重绘，旧位置与新位置分别失效，不进入场景 Overlay；
- CanvasView 统一保存工具光标，中键或 Space 平移结束后恢复当前工具光标；
- 单次极大笔划仍受现有 1 GiB PatchStore 预算约束。
