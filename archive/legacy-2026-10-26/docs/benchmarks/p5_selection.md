# P5 统一选区基准

- 日期：2026-07-24
- Python：3.14.3 x64
- 系统：Windows 10 22H2 x64
- 处理器：AMD64 Family 25 Model 80
- 物理内存：约 63.42 GiB

## 场景

在 4096×4096 空白文档上执行默认容差、连续 4 邻域、包含 Alpha 的魔棒全画布选择；
随后对居中的 2048×2048 硬边矩形选区执行 16 px 羽化。两项操作均调用正式的无 Qt
Worker 算法入口，测量完整计算和 Tile 输出时间。

## 结果

| 指标 | 结果 | 状态 |
| --- | ---: | --- |
| 4096 全画布魔棒 | 666.519 ms | PASS |
| 魔棒输出 | 256 Tile / 16 MiB | PASS |
| 4096、16 px 羽化 | 43.696 ms | PASS |
| 羽化变化输出 | 64 Tile / 4 MiB | PASS |
| 进程 RSS 净增量 | 1.89 MiB | RECORDED |

魔棒和调整操作在串行 JobRunner 中运行，主线程只捕获 copy-on-write 快照、更新进度并
在 UUID/revision 校验后提交，因此上述计算时间不会形成同等长度的主线程阻塞。8192
场景继续只保证正确性和可取消边界，不作为 P5 阻塞性耗时指标。

机器可读结果位于被忽略的 `build/benchmarks/p5.json`。

## 复现

```powershell
.\scripts\benchmark-p5.ps1
```
