# ADR-0023：sRGB 颜色调整与原子全分辨率预览

- 状态：Accepted
- 日期：2026-07-29

## 决策

1. P7A 颜色调整只作用于活动栅格层；活动选区按 `mask / 255` 混合调整前后结果。
2. 权威像素继续使用 straight RGBA、sRGB、`uint8` Tile。常规颜色调整保留 Alpha；
   反相、伽马、曲线、渐变和颜色替换可显式修改 Alpha。
3. LUT、HSL、渐变和颜色替换直接在 display-referred sRGB 字节上计算。调色板最近色
   使用 D65 CIE Lab 与 Delta E 76；可选 Floyd-Steinberg 抖动跨 Tile 连续。
4. 调整会话捕获不可变 `DocumentSnapshot`。参数变化经 50 ms 防抖后提交串行
   `JobRunner`，旧任务取消，只接受最新 generation、document、layer 和 revision。
5. 预览始终为全分辨率结果。新结果计算期间继续显示上一份完整结果；渲染协调器使用
   `replace_pixel_preview()` 在一次 frame generation 中替换完整映射，不经过清空和
   分批重建。
6. 预览不改变 Document、modified、自动恢复或撤销栈。应用时通过一次
   `apply_pixel_tiles()` 提交；取消、任务失败、文档变化或图层变化清除预览。
7. 超过 64 MiB 的调色板工作表面使用临时 memmap，并按行或 Tile 检查取消。

## 影响

- OpenGL 和软件后端共享相同的私有 Tile 结果，不需要 GPU 专用颜色算法。
- 参数快速变化可能跳过中间值，但不会显示部分更新或旧、新 Tile 混合。
- P7A 不引入调整图层、滤镜栈、直方图或项目格式字段。
