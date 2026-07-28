# P6R GPU 画布渲染基础重构验收

- 日期：2026-07-28
- 自动验收：Passed
- 人工验收：Passed
- 总体状态：Accepted

## 交付结果

- 已建立 `CanvasRenderCoordinator` 统一 generation 和预览入口；
- 已将 `CanvasItem` 替换为只提供文档几何的 `CanvasBoundsItem`；
- 已移除运行时画布对 composite/stale/progressive Tile 链的依赖；
- 已实现 OpenGL 3.3 Core 完整帧、2048 纹理页、预乘 source-over 和 512 MiB 软预算；
- 已实现 QPainter 完整帧软件参考后端和 OpenGL 自动回退；
- 已将画笔轮廓迁移到 `drawForeground()`，移除滚动 blit 与局部 repaint 修补；
- 已接入画笔私有 Tile、自由变换矩阵、选区 base/floating 和图层删除生命周期；
- 已增加自动/OpenGL/软件全局设置、CLI 覆盖和 render smoke；
- 已新增 ADR-0018，并将 ADR-0008 标记为已取代。

P6A 与 P6R 已完成联合人工验收，自由变换交互改进也已通过后续人工复核。

## 自动验收结果

```text
uv run ruff check .                         PASS
uv run mypy src                             PASS (63 source files)
uv run pytest -q                            PASS (218 tests)
uv run python -m easypicture --smoke-test   PASS
source software render smoke                PASS
source OpenGL render smoke                  PASS
Nuitka standalone build                     PASS
isolated PATH startup smoke                 PASS
isolated PATH I/O smoke                     PASS
standalone software render smoke            PASS
standalone OpenGL render smoke              PASS
```

本机 OpenGL 诊断：

- GPU：NVIDIA GeForce RTX 3060 Laptop GPU；
- OpenGL：3.3.0 NVIDIA 610.62；
- smoke 场景纹理页：32 MiB；
- 内容区域 GPU/软件最大逐通道误差：0 LSB；
- 文档装饰边框（不属于像素内容）最大差异：128 LSB，范围限制在边框两侧 2 px。

构建产物：
`build/nuitka/easypicture.dist/EasyPicture.exe`。

## Render smoke

render smoke 创建透明多图层场景，依次显示画笔预览、旋转出界、恢复原始姿态，并在
OpenGL native pass 内读回 framebuffer。内容区域与软件参考逐通道误差目标为
`<= 1 LSB`。OpenGL 与 QPainter 对文档装饰边框的覆盖规则不同，因此边框两侧
2 px 单独记录，不作为像素内容误差。

```powershell
uv run python -m easypicture --render-backend opengl `
  --render-smoke-test .cache\render-smoke-opengl
uv run python -m easypicture --render-backend software `
  --render-smoke-test .cache\render-smoke-software
.\scripts\smoke-render-standalone.ps1
```

输出：

- `rotated.png`
- `framebuffer.png`
- `reference.png`
- `diagnostics.json`

## 人工验收

1. `Passed`：反复旋出画布并旋回，所有边角完整恢复；
2. `Passed`：快速拖动四角、旋转按钮和属性数值，帧跟随且无旧姿态；
3. `Passed`：连续显隐、排序和拖动不透明度无闪烁；
4. `Passed`：快速画笔、橡皮、滚轮和中键平移无残影；
5. `Passed`：画布外预览及提交后画布外像素保留；
6. `Passed`：自动、OpenGL 和软件后端均可启动；
7. `Passed`：standalone 的 OpenGL/软件 render smoke。
