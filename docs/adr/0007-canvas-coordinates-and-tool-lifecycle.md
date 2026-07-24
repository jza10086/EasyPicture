# ADR-0007：画布坐标与工具生命周期

- 状态：Accepted
- 日期：2026-07-23

## 决策

画布使用 QGraphicsView/QGraphicsScene，一 scene unit 等于一个文档像素。文档左上角
为 `(0,0)`；坐标链固定为 global → viewport → scene → document → layer。Layer
坐标等于 Document 坐标减去整数 origin。

视图缩放范围为 6.25%～3200%，支持 Ctrl+滚轮、适合窗口和 100%；缩放保持鼠标锚点
稳定。场景拖拽留白按当前 viewport 的半宽和半高动态计算，使画布四条边在任意缩放下
都能移动到视口中心；画布适合某一方向时隐藏该方向滚动条，但保留内部滚动范围。
中键或 Space+左键只操作滚动条，不触碰像素或文档 revision。

工具实现统一 Protocol，由 ToolController 独占活动工具。工具切换顺序固定为：

1. deactivate 旧工具；
2. 清除全部 Overlay；
3. 释放鼠标捕获并恢复默认光标；
4. 设置并 activate 新工具。

Overlay 由独立高 z-order QGraphicsItemGroup 持有。P1 只提供 NullTool，正式工具从
P4 开始接入，不得把工具状态堆入 CanvasView。

## 原因

稳定的坐标边界是选区、变换、裁切和笔刷命中的共同基础。集中清理工具状态可以防止
切换工具后残留手柄、鼠标捕获或过期事件。

## 后果

- P2 合成结果仍由单一 CanvasItem 显示，不为每层创建 GraphicsItem；
- 适合窗口不再等同于锁定画布位置，用户仍可检查靠近窗口边缘的内容；
- 所有工具必须返回事件是否已消费；
- Overlay 只用于显示，不能写入权威像素。
