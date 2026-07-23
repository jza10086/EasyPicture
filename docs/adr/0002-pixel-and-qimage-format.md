# ADR-0002：straight RGBA 与预乘显示缓存

- 状态：Accepted
- 日期：2026-07-23

## 决策

领域像素唯一格式为 C-contiguous `uint8[H,W,4]`、RGBA 通道、straight Alpha、
sRGB 编码值。NumPy Tile 是权威数据。

Qt 渲染缓存使用 `QImage.Format_RGBA8888_Premultiplied`。桥接先借用 NumPy
缓冲区构造 straight RGBA QImage，再转换并返回拥有独立存储的预乘 QImage。
渲染缓存不得反向写回或重建权威像素。

## 原因

NumPy、OpenCV 和 Pillow 对 straight RGBA 的语义更直接，而 QPainter 对预乘格式
的 Alpha 合成更高效。显式桥接可以避免通道顺序、生命周期和预乘状态混用。

## 后果

- Alpha 为 0 的隐藏 RGB 可能在显示缓存中丢失，但权威 NumPy 数据保持不变；
- 预乘再反预乘存在量化误差，因此测试比较渲染结果而不是要求缓存往返逐字节相等；
- 所有 QImage 构造必须集中在渲染桥模块。
