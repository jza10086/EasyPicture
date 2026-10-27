# P7B 完整图片与静态 GIF 导出验收

- 日期：2026-07-29
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted

## 已实现

- PNG、JPEG、WebP、BMP、TIFF、TGA 和静态 GIF 统一导出；
- 全画布可见图层权威 CPU 合成，不读取显示缓存或临时 Overlay；
- PNG 压缩、JPEG 质量、WebP 无损/质量、TIFF 压缩和 TGA RLE；
- GIF 颜色数、Alpha 阈值、专用透明索引和可选抖动；
- JPEG/BMP 白色透明背景，规范化 sRGB ICC 与源元数据清理；
- 同目录临时文件、完整重开验证和 `os.replace` 原子覆盖；
- 中文 Blender 风格统一导出窗口、扩展名同步、能力探测和设置记忆；
- 导出进度、取消、覆盖确认以及不改变 modified/undo 的生命周期；
- 源码及 standalone 七格式 `--export-smoke-test`。

## 自动验收

```text
P7B 及受影响功能相关回归                  PASS (146 tests)
Ruff                                      PASS
mypy src                                  PASS
源码 smoke                                PASS
源码七格式 export smoke                   PASS
Nuitka standalone build                   PASS
isolated PATH startup + I/O smoke          PASS
standalone 七格式 export smoke             PASS
```

构建产物：`build/nuitka/easypicture.dist/EasyPicture.exe`。

## 人工验收

1. `Passed`：导出七种格式并用 Pillow 与 Windows 常用查看器打开；
2. `Passed`：检查透明格式及 JPEG/BMP 白色背景；
3. `Passed`：检查 GIF 颜色数、Alpha 阈值、抖动和静态单帧；
4. `Passed`：验证格式切换、参数记忆、扩展名和覆盖确认；
5. `Passed`：验证取消或失败不会损坏原文件；
6. `Passed`：确认导出不清除星号且不增加撤销命令；
7. `Passed`：检查较大图片导出期间的进度、取消和窗口响应。
