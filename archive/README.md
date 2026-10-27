# 归档索引

本次整理与验证结果见 [目录整理记录](migrations/2026-10-27-layout-report.md)。

- `legacy-2026-10-26/`：本次整理前的全部旧构建（含回滚源码/ZIP）、历史文档与报告、
  工具缓存、旧 Python 环境、原始配置和脚本。原内容保留，不作为当前发布文件。
- `validation/tests/`：原回归测试，仍由 `source/pyproject.toml` 和 `check.ps1` 调用。
- `validation/benchmarks/`：原性能场景。
- `validation/test-assets/`：原人工测试输入和工程。
- `migrations/2026-10-26-moves.json`：旧路径、新路径、文件数和字节数。
  搬迁前后比较相对路径和大小，不计算或验证摘要。
- `runs/`：新检查/构建按时间分别留档；包含报告、日志、smoke 输出和编译中间文件。
- `releases/`：以后发布时自动保留被替换的成品。

历史报告中的相对路径保留当时语境；查询当前路径请使用搬迁清单。
已有未提交源码改动随目录迁移保留，未做 reset、clean 或删除旧产物。
