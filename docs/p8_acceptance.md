# P8 本机维护性与日常体验验收

- 日期：2026-07-30
- 自动验收：Passed
- 人工验收：Partially passed
- 总体状态：Awaiting verification

## 已实现

- 2 MiB × 5 的 UTF-8 轮转日志；
- 主线程、工作线程和 Qt 消息异常记录；
- 启动环境、后台任务、文档 I/O、渲染回退及清理摘要日志；
- 应用专属临时根、任务所有权标记、24 小时遗留清理和正常退出清理；
- 原子保存及导出临时文件的精确目标清理；
- 最近 10 个文件、MRU 去重、QSettings 持久化和失效路径处理；
- 非模态提示去重、错误日志入口；
- 帮助菜单、日志目录入口、关于窗口和复制诊断信息；
- `version.py` 单一版本来源及 EasyPicture 0.1.0。

## 自动验收

```text
P8 维护、控制器、设置、窗口与持久化相关测试    Passed (153 tests)
Ruff                                           Passed
mypy src                                       Passed
源码 smoke                                     Passed
Nuitka standalone build                        Passed
isolated PATH smoke                            Passed
standalone I/O / export smoke                  Passed
```

构建产物：`build/nuitka/easypicture.dist/EasyPicture.exe`。

## 人工验收

1. `Passed`：检查日志生成、中文内容、轮转和日志目录入口；
2. `Passed`：模拟过期临时目录，确认只清理 EasyPicture 自有内容；
3. `Passed`：检查正常提示、重复警告抑制和错误日志入口；
4. `Passed`：打开多个图片和项目，检查最近文件顺序及重启恢复；
5. `Passed`：移动或删除最近文件，确认安全移除且当前文档不变；
6. `Pending`：重新检查关于窗口中的 PySide6、NumPy、Pillow 和 OpenCV
   运行时版本；Nuitka 不含 dist-info 时已改用模块自身的 `__version__`；
7. `Passed`：在当前机器 standalone 中复核以上核心流程。

## 不适用范围

- 其他操作系统或其他计算机；
- 安装器、自动更新、文件关联和代码签名；
- 干净 Windows 环境兼容矩阵；
- 8192 极端项目性能门槛和长时间压力测试。
