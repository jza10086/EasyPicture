# ADR-0015：选区轮廓、区域查询与后台任务

- 状态：Accepted
- 日期：2026-07-24

## 决策

蚂蚁线是独立于工具 Overlay 的持久 `QGraphicsItem`。工具切换只清理临时工具对象，不得
清除选区轮廓。轮廓以蒙版 `>=128` 为边界，使用屏幕 1 px 的黑白交替虚线，并以约
100 ms 周期更新相位。轮廓按可见 Tile 加一像素邻域生成和缓存；选区事件只失效变化
Tile 及其相邻 Tile。全实心内部 Tile不生成路径。

油漆桶和魔棒共用 document-space RGBA 参考表面构建与 `RegionQuery`。查询继续使用最大
通道绝对差；`include_alpha=True` 比较 RGBA，关闭时只比较 RGB。油漆桶保持包含 Alpha
的既有默认，魔棒在顶部选项栏提供显式开关。

魔棒和选区边缘调整通过串行 JobRunner 接收不可变 DocumentSnapshot。任务超过 150 ms
才显示应用模态进度对话框；Worker 不访问 Document、Widget、QImage 或 QUndoStack。
结果回到主线程后必须匹配 document UUID 和 revision，取消、失败或过期结果不提交。
4096 以上参考表面和单通道选区可使用临时 memmap。

## 原因

持久轮廓与工具 Overlay 分离，才能保证切换画笔或取消临时拖动时选区仍然可见。共享
区域服务避免魔棒和油漆桶在透明度、邻域和合并取样上产生不一致。快照与 revision
校验防止后台结果覆盖后续编辑。

## 后果

- 蚂蚁线不会进入权威像素、项目文件、缩略图或 PNG 导出；
- 画笔、橡皮和油漆桶继续直接消费同一个 SelectionMask 权重；
- 套索、快速蒙版、浮动选区和选区变换属于后续阶段。
