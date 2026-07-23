# ADR-0008：QPainter Tile 合成与渐进渲染

- 状态：Accepted
- 日期：2026-07-23

## 决策

P2 在渲染层把 straight RGBA Tile 转换为拥有独立内存的
`QImage.Format_RGBA8888_Premultiplied`，再使用 `QPainter` 按文档中的底层到顶层
顺序执行 source-over。图层显隐、不透明度、局部尺寸和任意整数 origin 均在此边界
处理；领域模型继续禁止持有 QImage。

渲染器使用两级 LRU：源 Tile QImage 以 layer id、Tile 坐标和 Tile revision 为键，
合成结果以文档 Tile 坐标为键。两级缓存共享 256 MiB 上限。

像素事件只失效受影响的源 Tile 及其覆盖的文档 Tile。图层排序、显隐和不透明度
变化清空合成结果，但不丢弃仍有效的源缓存。

CanvasItem 在 paint 中只绘制缓存命中项，未命中项保留棋盘格并加入以视口中心优先
的队列。主线程调度时间片目标为 8 ms；每轮至少完成一个 Tile 后归还事件循环。

## 原因

当前机器的探测中，NumPy straight RGBA 十层合成 P95 约 33 ms，而已桥接 QImage 的
QPainter 十层合成 P95 约 0.23 ms。QPainter 同时天然匹配 Qt 的预乘显示格式。

适合窗口时整个 4096 画布都可能位于 exposed rect。若在一次 paint 中同步生成所有
Tile，即使单 Tile 足够快也会造成明显冻结，因此必须渐进调度。

## 后果

- 渲染缓存是可丢弃派生数据，不能反向写回领域像素；
- P3 导出不能从显示缓存读取权威像素；
- 当前调度器只在主线程分时执行，不属于后台 JobRunner；
- 缓存规则或 QPainter 合成边界变化必须重新运行 P2 golden 和密集层基准。
