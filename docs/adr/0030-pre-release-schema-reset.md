# ADR-0030：首次发布前格式重置与兼容边界

- 状态：Accepted
- 日期：2026-08-01
- 取代：ADR-0010 的旧 manifest 结构与迁移承诺、ADR-0026/0027/0028 的开发期 schema 版本链

## 背景

EasyPicture 尚未投入正式使用。开发期先后出现过扁平栅格层、文字组、两级通用组和
SVG 子层等中间格式，也保留了重复快照字段、领域事件和渲染入口。继续维护这些过渡
接口会扩大测试矩阵，并让首次正式版本在发布前就背负无实际用户数据的迁移成本。

## 决策

当前 `.limg` 两级图层树重新编号为 schema v1。加载器只接受
`schema_version == 1` 且含合法 `nodes` 的项目；开发期 v1～v4 和未来版本均拒绝。
栅格组的 `children` 直接保存完整栅格层及 Tile 描述，矢量组只接受当前
`svg_text`/`svg_element` 子层。文字描边、换行方式等当前字段必须显式存在。

`DocumentSnapshot` 只保存 `nodes`，并提供从节点派生的只读 `raster_layers`；
`Document.layers` 是当前正式的扁平栅格视图。Document 只接受顶层组，所有栅格子层
必须与画布同尺寸且 origin 为零。项目保存、恢复、算法、导出和后台任务统一使用这套
结构，不再执行几何归一化或 schema 迁移。

领域事件统一为 `GroupListChanged`、`GroupMembersChanged`、
`GroupPropertiesChanged` 和 `VectorObjectChanged`。画布显示统一使用完整帧协调器；
旧 TileRenderer、合成/stale 缓存及 UI 属性别名不再保留。

## 保留边界

- 保留 ZIP 安全校验、Tile zlib 编码、SHA-256、原子替换、恢复草稿和未来版本拒绝；
- 保留 OpenGL/软件回退、字体回退、应用目录容错和导出扩展名别名；
- 保留标准嵌套 SVG 视口转换，以及对不支持内容的跳过和汇总警告；
- 普通图片和 SVG 是中立交换格式，不携带 EasyPicture 私有工程结构。

## 后果

现有开发期 `.limg` 不保证打开，这是首次正式使用前的一次性破坏性重置。正式发布后，
schema v1 成为兼容基线；后续若改变持久结构，必须新增版本和显式迁移，并用已发布项目
样本验证。旧 ADR 继续保留为开发历史，但不再构成当前格式契约。
