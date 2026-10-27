# P7A 非破坏性颜色调整验收

- 日期：2026-07-29
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted

## 已实现

- 反相、阈值、亮度/对比度、伽马、PCHIP 曲线；
- 色相/饱和度/明度、渐变映射和柔和颜色替换；
- D65 CIE Lab 最近色调色板和可选 Floyd-Steinberg 抖动；
- RGB、单通道及显式 Alpha 调整；
- 活动选区半透明权重、图层锁定和透明像素锁定；
- 50 ms 参数防抖、旧任务取消及最新 generation 校验；
- 全分辨率完整结果原子预览、前后预览切换和一次命令提交；
- 右侧非模态调整页、曲线/渐变/调色板编辑和画布颜色吸管；
- 数值编辑完成后可使用 Enter 或 Ctrl+Enter 应用当前调整；
- QSettings 参数持久化，临时状态不进入 `.limg`。

## 自动验收

```text
P7A 算法、会话与原子渲染定向测试       PASS (24 tests)
受影响命令/选区/设置/持久化/UI 回归    PASS (119 tests)
Ruff 修改范围                           PASS
mypy 修改范围                           PASS
源码 smoke                              PASS
源码软件/OpenGL render smoke            PASS (maximum error 0)
Nuitka standalone build                 PASS
isolated PATH startup + I/O smoke       PASS
standalone software render smoke        PASS (maximum error 0)
standalone OpenGL render smoke           PASS (maximum error 0)
```

OpenGL render smoke 必须允许测试窗口实际显示；使用 `Start-Process -WindowStyle Hidden`
会阻止 `QOpenGLWidget` 呈现内容，只能抓到清屏帧，并非渲染结果。

构建产物：`build/nuitka/easypicture.dist/EasyPicture.exe`。

## 人工验收

1. `Passed`：检查九类调整及 RGB、单通道和 Alpha 行为；
2. `Passed`：快速连续拖动参数，确认完整帧更新且无闪烁或旧 Tile；
3. `Passed`：验证曲线、渐变、颜色吸管、调色板和抖动；
4. `Passed`：验证半透明选区、图层锁定和透明像素锁定；
5. `Passed`：验证前后预览、取消、过期结果和一次应用一次撤销；
6. `Passed`：在代表性较大图片上检查进度、响应和取消；
7. `Passed`：重启确认参数恢复，保存重开确认结果为普通栅格像素。
