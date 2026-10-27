# EasyPicture 源代码

Windows x64 / Python 3.14 / PySide6。栅格、文字和 SVG 使用统一对象模型，
工程格式仅接受 schema v5。对象局部像素与画布输出范围分离，画布裁切保留源内容。

从仓库根目录执行：

```powershell
.\source\scripts\sync.ps1       # 安装锁定依赖
.\source\scripts\run.ps1        # 开发运行
.\source\scripts\check.ps1      # 静态检查、归档回归测试与离屏 smoke
.\source\scripts\package.ps1    # 编译、验证并发布 release/EasyPicture.exe
.\source\scripts\smoke-standalone.ps1
```

也可进入本目录执行 `uv run python -m easypicture` 或 `uv run python -m pytest`。
历史测试位于 `../archive/validation/tests`；pytest 配置保留自动发现和共享测试导入。
开发环境及工具缓存只写入 `.venv/`、`.cache/`，验证报告及构建中间文件写入
`../archive/runs/`，发布文件只写入 `../release/`。

- [对象架构](docs/object_architecture.md)
- [独立栅格编辑页与创建集合规则](docs/raster_editor.md)
- [工程格式](docs/limg_schema_v5.md)
- [人工交互验收](docs/object_manual_acceptance.md)
- [目录与构建约定](docs/project_layout.md)
- [历史五阶段报告](../archive/legacy-2026-10-26/docs/object_refactor_progress.md)

自动验证不进行文件或资源摘要校验，不操作 Windows 原生鼠标。
原生鼠标手感与硬件 OpenGL 仍由用户验收。
