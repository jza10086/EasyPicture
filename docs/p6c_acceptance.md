# P6C 基础图形工具验收

- 日期：2026-07-28
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted

## 已实现

- 直线、矩形、圆角矩形、椭圆和凹多边形；
- 独立描边/填充颜色、三种绘制样式、不透明度、抗锯齿及五种混合模式；
- 选区权重、图层锁定、透明像素锁定和 straight RGBA 语义；
- 4 倍超采样、Tile padding 与跨 Tile 无缝栅格化；
- 鼠标创建、Shift 约束、Alt 中心创建和多边形逐点闭合；
- 固定屏幕尺寸橙色 Overlay、端点/八手柄/半径/顶点编辑和内部移动；
- 右侧精确属性及多边形顶点选择；
- 会话内撤销/重做、拖动右键复位、Esc 取消、Enter/双击确认；
- 私有像素预览、单命令提交及文档/图层/revision 生命周期保护；
- 超过 400 万像素或 64 Tile 的可取消后台预览；
- 图形类型、样式、颜色和参数 QSettings 持久化。

验收修复：手柄拖动开始时立即撤下上一姿态的精确像素预览，拖动期间使用当前描边/
填充颜色的轻量矢量预览；松手或右键恢复后重新生成精确 Tile。多 Tile render smoke
现会在预览仍存在时比较六个 Tile 的 OpenGL 帧与软件参考，防止旧姿态与新边界重叠。
后续进一步将预览 revision 改为跨 `clear` 单调递增，避免“清除并在同一帧重建”时
OpenGL 纹理页误用旧签名；轻量预览在鼠标按下的同一事件内建立，并保持到精确 Tile
完成上传，消除拖动首帧闪烁。
数值属性拖动也复用相同会话阶段：`interactionStarted` 撤下精确 Tile，连续
`valueChanged` 只更新轻量 Overlay，`editingFinished` 后才进行一次精确栅格化。
自由变换规则模式同步回写受约束联动的非活动角点字段，避免连续拖动时用过期角点反复
推导造成闪烁或预览停更。
矩形、圆角矩形和椭圆的精确属性现在保留创建起点作为 `X/Y` 锚点，并以第二角点
相对起点的有符号差值显示 `W/H`；宽高跨过零时不会再因规范化边界而额外移动锚点。
数值输入状态机也将组件边缘和浮点外壳统一归入活动文本会话，二次点击只移动文本光标
或选择文本，不会抢走焦点并在下一次操作中误入拖动状态。
针对真实双击序列继续收紧事件所有权：文本编辑器现在覆盖数值框完整命中区域，视觉内边距
改由编辑器自身提供；整个控件在文本态均使用 I 型光标。拖动状态还会拒绝不带左键状态的
滞后鼠标移动，避免鼠标回卷或上一点击残留事件把后续点击误判为拖动。
最终改为 Blender 式半模态按钮处理：新增独立 `TEXT_SELECTING` 状态，活动文本会话
在窗口级统一接管连续点击；投递到整数外壳、浮点包装或文本编辑器的框内鼠标事件都被
规范化到同一个 `QLineEdit`，不再依赖 Qt 在连续点击期间重新进行子控件命中测试。

## 自动验收

当前定向覆盖：

- 几何退化、自交拒绝、凹多边形和圆角钳制；
- 独立描边/填充、选区权重、透明像素锁定和全部混合模式；
- 画布裁切、抗锯齿与 Tile 接缝；
- 临时预览、右键恢复、会话历史、单命令提交和图层变化取消；
- 属性面板、设置持久化和多边形 Enter 闭合。

最终受影响回归：

```text
uv run ruff check .                         PASS
uv run mypy src                             PASS (70 source files)
P6C/画笔/选区/变换/渲染/命令/持久化回归     PASS (104 tests)
uv run python -m easypicture --smoke-test   PASS
Nuitka standalone build                     PASS
isolated PATH startup + I/O smoke            PASS
standalone software render smoke             PASS
standalone OpenGL render smoke               PASS
6 Tile preview OpenGL/software comparison    PASS (maximum error 0)
same-frame 6 Tile preview replacement         PASS (maximum error 0)
```

构建产物：`build/nuitka/easypicture.dist/EasyPicture.exe`。

## 人工验收

1. `Passed`：绘制五种图形并检查描边、填充和独立颜色；
2. `Passed`：Shift、Alt、移动、八手柄、圆角半径和多边形顶点；
3. `Passed`：右侧精确属性及连续数值拖动；
4. `Passed`：会话内撤销、右键恢复、Esc 取消和 Enter 单次提交；
5. `Passed`：选区、锁定、混合模式和多图层遮挡；
6. `Passed`：缩放、平移及较大预览期间的响应与残影；
7. `Passed`：参数重启恢复以及保存重开后的普通栅格像素。
