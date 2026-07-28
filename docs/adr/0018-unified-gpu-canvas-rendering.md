# ADR-0018：统一完整帧与 OpenGL/软件双后端

- 状态：Accepted
- 日期：2026-07-28
- 取代：ADR-0008

## 背景

旧画布由 `CanvasItem.paint()` 请求文档 Tile 合成缓存，并用 dirty rect、stale
composite、渐进队列和滚动位图修补局部更新。自由变换允许内容超出文档边界后，
这一结构同时受 `QGraphicsItem.boundingRect()`、旧 framebuffer 和多个缓存
generation 约束；旋出画布再旋回可能留下截断或暗块。继续添加局部失效补丁会扩大
状态组合，无法把“当前显示必定对应当前状态”作为不变量。

## 决策

`CanvasRenderCoordinator` 成为所有显示状态的唯一入口。文档、画笔私有 Tile 或
变换矩阵发生变化时只增加 frame generation 并请求完整 viewport 更新，不计算
屏幕脏矩形，不保留最终合成 Tile，也不允许显示旧 generation。

`CanvasView` 保留 `QGraphicsView` 的坐标、缩放、平移和 Overlay API，并使用
`FullViewportUpdate`。原 `CanvasItem` 替换为不绘制像素的 `CanvasBoundsItem`。
栅格内容在 `drawBackground()` 中完成；画笔轮廓在 `drawForeground()` 中绘制。

后端分为：

- OpenGL 3.3 Core：使用 `QOpenGLWidget`、shader 和 2048×2048 纹理页；
- 软件参考：用 QPainter 直接按底到顶绘制 revision 化源 QImage。

OpenGL 每帧显式清空 framebuffer，GPU 页保存预乘 RGBA，混合因子固定为
`ONE / ONE_MINUS_SRC_ALPHA`。正常图层裁切到文档，活动变换预览不裁切。普通图层
使用 nearest 显示采样；变换预览的 nearest/linear 由工具插值选项映射。最终变换、
导出、缩略图和保存仍使用 CPU 权威服务。

纹理页缓存软预算为 512 MiB。当帧内可见资源或活动变换资源超过预算时允许暂时
超限，禁止用空白或旧页替代当前帧。优先驱逐非当前 generation 页面，并在文档
替换、图层删除或 OpenGL context 销毁时主动释放资源。

OpenGL 初始化或绘制失败时切换到软件后端，并通过状态栏报告一次原因。全局设置可选
自动、OpenGL 或软件，修改后重启生效。`--render-backend` 可进行进程级覆盖。

## 预览和生命周期

- 画笔/橡皮只替换对应源 Tile 纹理，取消后从权威 Tile 恢复；
- 自由变换只更新矩阵，源纹理不随鼠标移动重新生成；
- 活动选区按 base/floating 两种源图像在原层级拆分；
- 删除活动层、切换文档或 revision 过期时，工具和协调器都清除预览；
- Overlay 不进入 framebuffer 权威数据，不参与保存或导出。

## 后果

- 完整帧正确性优先于 dirty rect 性能，适合日常不超过 4096 的使用范围；
- 旧 progressive/stale/composite 链不再属于运行时画布；
- 软件后端是故障回退和 golden 参考，不是另一套状态模型；
- GPU 显示结果不得反向写回 Document；
- OpenGL 与 QPainter 的画布边框线段栅格可能相差 1～2 个屏幕像素，内容 golden
  必须排除装饰边框后比较。
