# 矢量与位图编辑界面

## 布局

参考 Affinity Designer 的 Persona 与 Context Toolbar 分工，主窗口左上设置
“矢量编辑”和“位图编辑”两个入口。矢量界面处理对象、集合、文字和图形；
选中栅格对象进入位图界面后，使用对象专属标签页编辑局部像素，工具栏只显示像素工具。
两种界面共用工程历史与保存，不生成副本，也不改变 schema v5。

- 左侧：当前工作区的图标工具栏，悬停显示工具名和快捷键。
- 顶部：当前工具的设置；“详细设置”展开角点、图形几何、切片及处理任务等复杂参数。
- 右上：矢量界面的对象大纲，按对象类型显示图标。位图界面隐藏对象大纲。
- 右下：对象属性，包含不透明度、锁定、对象变换、文字或 SVG 内容属性。
  “颜色”标签整合原前景色／背景色面板；位图界面默认显示该标签。

选择对象更新右侧属性，切换工具更新顶部参数，二者互不抢占。
旧的浮动面板布局版本失效，重新使用本次布局；工程内容不迁移。

## 导航与选择

滚轮直接以指针位置为中心缩放；中键或 Space 拖动继续用于平移。
小幅滚动按增量比例缩放，零纵向滚动不改变视图。

对象选择工具顶部提供“忽略透明像素”，偏好持久保存，默认关闭。

- 关闭：按对象范围选取，透明或新建的空栅格区域也能选中。
- 开启：点选跳过有效不透明度为零的对象，以及点击处透明的像素／SVG／文字内容。
  栅格通过逆变换读取局部像素 Alpha，SVG 和文字仅渲染命中点的单像素覆盖率。
- 任意非零 Alpha 都可命中。框选保持按对象范围选择，不受该开关影响。
- 显隐、集合继承、锁定和多选规则保持原有语义；选择偏好不进入撤销历史。

## 图标与许可

使用 Lucide 线性 SVG 图标，24 像素工具栏、20 像素大纲，颜色区分集合、
栅格、文字和矢量图元。图标随程序本地打包，无联网加载或字体图标依赖。
大纲已移除像素缩略图生成和内容失效逻辑；精灵帧缩略图不受影响。

图标来源：<https://github.com/lucide-icons/lucide/tree/main/icons>，获取日期 2026-10-27。
完整 ISC 许可及派生 Feather 图标的 MIT 许可见
`src/easypicture/resources/icons/LICENSE.txt`，同 SVG 一起嵌入 EXE。

## 参考

- [Affinity Designer 界面参考](https://affinity.help/designer2/English.lproj/pages/Workspace/interface.html)
- [Affinity 当前界面分区](https://www.affinity.studio/help/workspace-interface/)
- [Blender Outliner](https://docs.blender.org/manual/en/latest/editors/outliner/introduction.html)：按对象类型识别的设计参考。
- [Lucide 许可](https://lucide.dev/license)

## 人工验收

自动验证采用 Qt offscreen 和虚拟指针，不执行 Windows 原生鼠标操作，不进行文件或资源哈希校验。
请人工检查两种工作区切换、右侧属性滚动、顶部连续数值拖动、滚轮缩放手感、
透明区域穿透、空栅格选择、文字／SVG 内容编辑、不同 DPI 与硬件 OpenGL 显示。
