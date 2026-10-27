# P10D 验收报告

## 状态

Accepted，人工验收通过。

实现及验收日期：2026-08-30。版本：0.6.0；项目格式仍为 schema v4。用户已确认人工验收完成，本阶段创建独立 Git 提交。

## 已实现

- 精灵帧右键“在 Tile 工作区检查”及每工程唯一只读二级标签。
- Tile A/B 一次性拾取，linked/detached 帧统一读取最新权威合成。
- 三种模式：平铺（3×3/5×5，含整数循环偏移）、相邻对比、过渡预览。只显示当前模式使用的属性及标签。
- 数值和枚举属性框右键菜单“设为默认值”恢复预设；数值正在拖动或文本编辑时右键只取消本次修改。
- RGB/RGBA 容差接缝统计和不进入权威像素的热力图 Overlay。
- 硬切与 Bayer 4×4 有序抖动，保持完整 RGBA 和确定性结果。
- 单命令创建未分组 detached 过渡帧，支持撤销/重做和后续完整编辑。
- Tile 参数 QSettings 往返；A/B 选择和标签状态不进入项目。
- 大 Tile 后台全分辨率准备、最新请求合并、进度及取消，旧帧保留到新结果完整到达。
- 重复像素块共享、选中帧内容缓存；删帧、关标签和切换工程均丢弃过期结果。
- 修复启用单格网格时独立帧/Tile 表面访问不存在工作区的异常；新增实际显示窗口回归。

## 自动检查

- P10D 算法、命令和 UI 定向测试：Passed。
- P10B/P10C 工作区、动画、精灵面板回归：Passed。
- Ruff 与 mypy：Passed（108 个源码文件）。
- 最新完整 pytest（模式精简与默认值菜单后）：503 passed（50.87 s）。输入控件、SVG、变换与 Tile 定向测试 61 passed；菜单最终新增 10 项测试均通过。
- 源码启动、I/O、七格式/单帧 PNG/SVG 导出：Passed。
- 源码软件/OpenGL Tile 像素及最终热力图截图：Passed（Tile 对比误差 0 LSB）。
- Nuitka standalone 构建：Passed（2026-08-30，341 个 C 文件）。
- 最终 exe 隔离 PATH 启动、I/O、七格式/单帧 PNG/SVG 导出：Passed。
- 最终 exe 隔离 PATH 软件/OpenGL render smoke：Passed；Tile 像素和热力图最大误差均为 0 LSB，原有矢量场景最大误差为 1 LSB（按既有规则排除画布边框）。

交付入口：`build/nuitka/easypicture.dist/EasyPicture.exe`。
初版双后端诊断：`.cache/p10d-final-standalone-software/diagnostics.json` 与 `.cache/p10d-final-standalone-opengl/diagnostics.json`。模式精简后的源码双后端诊断：`.cache/tile-mode-software/diagnostics.json` 与 `.cache/tile-mode-opengl/diagnostics.json`，Tile 像素及热力图均为 0 LSB。三模式实际 Qt 面板截图：`.cache/tile-panel-modes.png`。
本机测试工程：`.cache/p10d-test-tiles.limg`，包含无缝草地和边缘差异沙地两个帧；缓存测试资产不纳入 Git。

模式精简与默认值菜单的最终 standalone 已重新构建（341 个 C 文件，329 缓存命中、12 重新编译）。隔离 PATH 启动、I/O、七格式/单帧 PNG/SVG 导出和双后端 render smoke 全部 Passed。最新 exe 的双后端诊断为 `.cache/tile-menu-standalone-software/diagnostics.json` 与 `.cache/tile-menu-standalone-opengl/diagnostics.json`；Tile 像素和热力图误差均为 0 LSB。

构建工具仍提示 Nuitka 4.1.3 对 Python 3.14 属实验性支持；本次保持既定 Python 3.14 技术栈，未更改依赖版本，并实际执行打包程序的 smoke。

## 人工验收

1. 使用无缝和明显接缝 Tile 检查 3×3/5×5 平铺：Passed。
2. 左右、上下边缘移到中心并检查循环偏移：Passed。
3. 调整容差、Alpha 和热力图并核对统计：Passed。
4. 选择两个 Tile 并检查四方向相邻与过渡：Passed。
5. 生成硬切和有序抖动过渡，确认没有新增混合颜色：Passed。
6. 打开生成的独立帧手工修整，确认源 Tile 不变：Passed。
7. 撤销/重做生成操作，保存重开并导出单帧与 Atlas：Passed。

追加验收：平铺/循环偏移模式合并、按预览模式显示属性，以及数值/枚举右键“设为默认值”和编辑中右键取消：Passed。验收依据为用户于 2026-08-30 的明确确认。

## 人工操作入口

1. 在精灵帧缩略图上右键，选择“在 Tile 工作区检查”。工程下出现唯一“Tile 检查”二级标签。
2. 右侧选择预览模式；点击“选择 Tile A/B”，再点击目标帧缩略图，Esc 可退出选择。
3. “平铺”可选 3×3/5×5，可按 X/Y 调整循环偏移或点击“边缘移到中心”，零偏移即普通平铺。仅使用 Tile A，隐藏 Tile B 和过渡参数；统计按“统计方向”比较，热力图同时显示横纵接缝。
4. “相邻对比”显示 A/B；“过渡预览”显示 A/结果/B。这两种模式隐藏平铺规模、偏移及偏移按钮；仅过渡模式显示过渡方式、边界和生成按钮，硬切还隐藏过渡宽度。切换模式再返回时参数保留。
5. 点击“生成独立过渡帧”，自动打开未分组的新独立帧，修改它不影响 A/B。检查一次撤销/重做完整删除/恢复新帧。
6. 大 Tile 计算时应保留上一完整画面，可取消；切换工程、删除输入帧后不得出现旧结果。缩放和平移不应因参数刷新漂移。
7. 将偏移改为非零、边界改为其他百分比；结束输入后右键“设为默认值”，应恢复为 0 / 50%。过渡宽度恢复为 min(8, 对应方向单格尺寸)。编辑过程中右键只能取消，不应弹菜单；恢复 SVG 属性后应可一次撤销。
