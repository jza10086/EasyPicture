# 目录与发布约定

仓库源代码、归档和发布产物分开；当前测试按约定集中在 `tmp/test/`：

```text
EasyPicture/
├─ source/
│  ├─ src/easypicture/     # 当前应用代码
│  ├─ scripts/            # 开发、检查、打包入口
│  ├─ docs/               # 当前架构和格式契约
│  ├─ pyproject.toml
│  ├─ uv.lock
│  ├─ .venv/              # 本机开发环境，Git 忽略
│  └─ .cache/             # 开发工具缓存，Git 忽略
├─ archive/
│  ├─ legacy-2026-10-26/  # 整理前材料，不删除旧工程或构建
│  ├─ validation/         # 历史报告及输入素材
│  ├─ migrations/         # 路径迁移清单
│  ├─ runs/               # 构建记录及历史检查报告
│  └─ releases/           # 后续发布时保留被替换的成品
├─ tmp/test/
│  ├─ tests/              # 当前回归测试
│  ├─ benchmarks/         # 性能场景
│  ├─ cache/              # 检查缓存，Git 忽略
│  └─ runs/               # 测试数据、日志、截图和报告，Git 忽略
└─ release/
   └─ EasyPicture.exe
```

根目录 README、`.gitignore`、`.git/`、`.agents/` 是仓库和工具元数据。
源代码迁移不改变应用对象模型、schema v5 或用户工程数据。

## 开发与回归

所有 PowerShell 入口以自身位置定位工作区，支持从其他当前目录调用。
`sync.ps1` 用锁定依赖创建 `source/.venv`；其他入口调用该环境的 Python，
不依赖旧目录的可执行脚本或系统 Python 包。
pytest 的 `testpaths` 和 `pythonpath` 指向 `tmp/test/tests` 及当前源码。
Ruff 显式声明项目包与测试包，避免目录搬迁改变导入分类。

`check.ps1` 将单元、UI、CLI 集成用例放在三个独立进程中完整执行，分别保留文本日志。
当前 Windows / PySide6 6.11.1 环境中，全部用例合并到一个进程，或启用 JUnit XML 收集时，
Qt 对象回收阶段可出现 `0xc0000374` 堆异常；未修改的 v0.9.0 同样复现。
默认按组独立运行并保留文本日志，不代表已修复合并运行／XML 收集的生命周期问题。
没有跳过用例或失败后自动重试。

检查输出进入 `archive/runs/check-时间/`：Ruff、mypy、完整 pytest、JUnit 和
应用启动、v5 读写、图片导出、SVG、软件渲染五项离屏 smoke。
性能入口为 `run-benchmark.ps1 -Scene objects`，也支持 interaction 和历史 p0/p2/p3/p4/p5。
历史性能入口的算法保持原样，执行前应核对其适用阶段。

## 发布

`package.ps1` 使用 Nuitka onefile 和 MSVC 构建当前源码。运行时库、Qt 插件、
依赖许可文件嵌入 EXE；单文件启动会解包到临时目录，正常退出后清理。
许可原文亦保留在对应构建的 `licenses/` 中，不复制开发工具到发布目录。

编译源码、目标文件、诊断、日志和 standalone 临时目录均留在
`archive/runs/package-时间/`。构建失败不替换已有发布文件。
候选 EXE 在独立目录运行五项 smoke，PATH 仅包含 Windows 系统目录，
使用 offscreen Qt，不发送原生鼠标事件。全部通过后，旧发布文件先归档，
新 EXE 再移入 `release/`。`smoke-standalone.ps1` 可复查已发布 EXE；
原 smoke-export/render 入口统一委托该协议。

发布目录不包含 ZIP、安装器、源码、测试、报告、DLL 散件或编译中间文件。
编译版本号来自 `src/easypicture/version.py`，不因目录整理擅自改版本。
该流程不做代码签名；原生交互和硬件 OpenGL 未由离屏测试验收。
搬迁与发布验证均不计算、比较或验证文件/资源摘要。
