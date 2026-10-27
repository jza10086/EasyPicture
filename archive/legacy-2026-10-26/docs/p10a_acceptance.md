# P10A 验收报告

## 状态

`Automated checks in progress / Pending manual acceptance`

目标版本：EasyPicture 0.3.0；项目格式：`.limg schema v2`。

## 已实现

- Qt 无关 `SpriteWorkspace` 与 `SpriteFrameDefinition`，统一单格尺寸、名称唯一、重复区域保护和最多 1024 帧。
- 帧元数据命令、统一撤销、自动恢复事件和几何操作原子清空/恢复。
- 像素网格、单格网格、吸附、像素精确坐标设置和固定尺寸切片 Overlay。
- 画布内确定按钮、方向键逐像素、Shift 单格步进、Alt 临时取消吸附及属性栏整数 X/Y。
- 底部帧条、惰性缩略图、重命名、删除、拖动排序、锚点和单帧 PNG 原子导出。
- `.limg` v1→v2 迁移及 v2 往返；追加工程不追加来源帧定义并发出提示。

## 人工验收

1. 16×16、32×32 和自定义单格尺寸、像素/单格网格：`Pending`
2. 逐像素拖动、属性 X/Y、方向键、Shift 与 Alt：`Pending`
3. 连续确认多个帧且框不跳动、原图不变：`Pending`
4. 帧条缩略图、重命名、删除、排序和已有帧更新：`Pending`
5. 源图修改后缩略图与单帧 PNG 更新：`Pending`
6. 锚点预设、整数坐标与网格吸附：`Pending`
7. schema v2 保存重开、恢复及几何清空/取消/撤销：`Pending`

人工验收通过后将总体状态改为 `Accepted`，并发布 0.3.0。
