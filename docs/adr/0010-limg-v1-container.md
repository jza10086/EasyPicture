# ADR-0010：`.limg` v1 容器与安全边界

- 状态：Accepted
- 日期：2026-07-23

## 决策

`.limg` v1 是 ZIP 容器，包含 `manifest.json`、`thumbnail.png`、按 UUID/坐标命名
的图层 RGBA Tile，以及稀疏 selection mask Tile。权威 Tile 使用 zlib level 1
压缩，ZIP 条目使用 STORE；manifest 保存原始长度和 SHA-256。

保存捕获 copy-on-write `DocumentSnapshot`，在目标同目录写临时文件，flush/fsync
并完整重读校验后通过 `os.replace` 替换。失败或取消不得改动原文件。

读取不解压到文件系统，并拒绝路径穿越、绝对/反斜杠路径、重复或未声明条目、未来
schema、越界尺寸/坐标、错误长度、异常压缩比、尾随 zlib 数据和校验失败。画布上限
8192×8192、图层上限 256、manifest 上限 4 MiB、解码像素总量上限 4 GiB。

## 原因

按 Tile 保存保留稀疏性和 Alpha 为 0 时的 RGB 数据，也避免保存时创建每层整图副本。
完整复核与同目录原子替换保证中断不会破坏已有项目。显式 schema 和迁移注册入口使
格式能够演进且不依赖 Pickle。

## 后果

- QImage、渲染缓存、撤销历史和运行时 revision 不进入项目；
- selection v1 即可保存，P5 不需要改变容器基础结构；
- 未知未来 schema 明确拒绝；旧版本只能通过显式迁移函数升级；
- 普通项目不能携带恢复草稿路径，恢复元数据只由专用启动恢复入口解释。
