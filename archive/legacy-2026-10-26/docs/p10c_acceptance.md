# P10C 验收报告

## 状态

Accepted。

## 已实现

- 精灵动画模型、独立 FPS、每帧保持拍数和单次/循环/往返播放模式。
- 动画只读画布标签、播放工具条、逐帧控制和总时长显示。
- 前后帧洋葱皮的数量、颜色、透明度和衰减设置。
- 精灵切片半透明参考帧、自动最近帧、手动拾取及安全清理流程。
- `.limg schema v4`、v3→v4 迁移和 Atlas JSON v2。
- FPS、拍数、动画排序和跨动画移动的统一撤销命令。

## 自动检查

- P10C 动画、洋葱皮、UI、持久化、Atlas 与设置定向回归：Passed。
- Ruff：Passed。
- mypy：Passed。
- 完整 pytest：440 Passed。
- 源码启动、I/O、七格式、SVG、软件/OpenGL render smoke：Passed。
- Nuitka standalone 构建及隔离 PATH 的启动、I/O、SVG、七格式和软件/OpenGL render smoke：Passed。

回归期间修复了同步前台任务完成信号早于 Qt runnable 完全退出导致的 Windows 线程池销毁竞态；控制器测试及完整回归均已复测通过。

## 人工验收

1. 将现有分组作为待机、移动、攻击动画使用：Passed。
2. 设置不同 FPS 和每帧保持拍数，检查实际播放节奏：Passed。
3. 验证单次、循环和往返播放：Passed。
4. 调整前后洋葱皮数量、颜色、透明度和衰减：Passed。
5. 检查动画与切片参考预览中的红色锚点标记、半径调节，并确认锚点不改变帧的像素位置：Passed。
6. 在切片工具中检查最近创建帧默认参考、透明度调节和手动拾取：Passed。
7. 编辑引用源图和独立帧，确认动画预览及时更新：Passed。
8. 保存 v4、重开并导出 Atlas PNG + JSON，确认动画数据完整：Passed。
