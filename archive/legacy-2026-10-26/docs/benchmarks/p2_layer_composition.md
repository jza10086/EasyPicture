# P2 图层合成与渐进调度基准

- 日期：2026-07-23
- Python：3.14.3 x64
- 系统：Windows 10 22H2 x64
- 处理器：AMD64 Family 25 Model 80
- 物理内存：约 63.42 GiB

## 场景

实际创建 4096×4096 文档和 10 个全尺寸密集 RGBA 图层。每层包含 256 个
256×256 Tile，共 2560 个已分配 Tile、640 MiB 权威像素。

## 结果

| 指标 | 结果 | 门槛 | 状态 |
| --- | ---: | ---: | --- |
| 密集层分配时间 | 1294.769 ms | 记录项 | PASS |
| 密集层 RSS 增量 | 643.15 MiB | 记录项 | PASS |
| 10 层冷缓存单 Tile 中位数 | 1.360 ms | 记录项 | PASS |
| 10 层冷缓存单 Tile P95 | 2.250 ms | ≤ 16 ms | PASS |
| 256 Tile 全画布合成 | 1881.911 ms | 渐进执行 | PASS |
| golden 最大误差 | 0 LSB | ≤ 1 LSB | PASS |
| 渐进调度最大 heartbeat 间隔 | 22.760 ms | < 100 ms | PASS |
| 渲染缓存峰值 | 256 MiB | ≤ 256 MiB | PASS |

全画布合成不是一次 UI paint 内同步执行。适合窗口等场景由 8 ms 目标时间片渐进
生成，本次 256 Tile 总用时 1714.314 ms，期间事件循环最大观测间隔为 22.760 ms。

完整机器可读结果位于被忽略的 `build/benchmarks/p2.json`。

## 复现

```powershell
.\scripts\benchmark-p2.ps1
```
