# ADR-0024：完整图片导出、静态 GIF 与原子替换

- 状态：Accepted
- 日期：2026-07-29
- 扩展：ADR-0005

## 决策

EasyPicture 从 CPU 权威 `DocumentSnapshot` 合成完整画布，并通过同一个
`export_image()` 入口导出 PNG、JPEG、WebP、BMP、TIFF、TGA 和静态 GIF。GPU
framebuffer、临时预览、选区轮廓及其他 Overlay 永不参与导出。

导出像素固定为 8 位 display-referred sRGB：

- PNG、WebP、TIFF 和 TGA 保留合成结果的 Alpha；
- JPEG 与 24 位 BMP 将透明度合成到纯白色；
- PNG、JPEG、WebP 和 TIFF 写入重新生成的 sRGB ICC；
- 不传播源 EXIF、XMP、GPS、缩略图、动画或注释；
- 导出成功不改变文档 revision、modified、撤销 clean index 或恢复草稿。

静态 GIF 最多使用 256 个调色板索引。Alpha 小于用户阈值的像素写入一个专用
透明索引，其余像素完全不透明；颜色数包含透明索引。调色板只从不透明像素生成，
可选 Floyd–Steinberg 抖动。文件固定为一帧且不写循环或应用扩展。GIF89a 的透明
图形控制扩展自身包含值为零的延时字段，Pillow 重新打开时可能报告
`duration == 0`，这不表示文件包含动画。

每次导出在目标同目录写入随机临时文件，完成 flush/fsync 和 Pillow 重开验证后，
才用 `os.replace` 替换目标。取消、编码失败或验证失败均删除临时文件并保留原目标。

## 原因

统一入口避免各格式分别实现图层合成、取消和错误处理。重新生成色彩信息并丢弃源
元数据，既符合项目已经转换到 sRGB 的事实，也避免把方向、GPS 或旧 ICC 错误附回
编辑后的像素。原子替换保证覆盖现有文件时不会因失败留下半个图片。

## 后果

- 普通图片始终是扁平化结果，只有 `.limg` 保留图层与项目状态；
- GIF 不能保留半透明、隐藏 RGB 或 ICC；
- Pillow 的单次编码调用无法中途打断，取消会在编码返回后阻止最终替换；
- 16 位、CMYK、HDR、动画 GIF、导出选区和导出时缩放不属于 P7B。
