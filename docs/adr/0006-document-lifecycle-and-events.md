# ADR-0006：单文档生命周期与领域事件

- 状态：Accepted
- 日期：2026-07-23

## 决策

V1 使用单窗口、单活动文档。`Document.create()` 接受 1～8192 的宽高，创建一个
透明“图层 1”和空 Selection；新文档初始为 clean。

Document、RasterLayer、Selection、几何值和领域事件不得导入 Qt。Document 对外
返回只读的图层 tuple，通过同步订阅接口发送不可变 dataclass 事件。订阅返回
unsubscribe，可重复调用。

真实内容修改调用 `record_change()`：递增 Document revision，并在首次修改时进入
modified。活动层变化会递增 revision 但不进入 modified。`mark_clean()` 只更新
clean 状态，不伪造内容 revision。

P3 保存实现前，modified 文档的新建、关闭和退出流程只提供“放弃更改/取消”。
取消必须保留原文档；新文档对话框取消也不得替换当前文档。

## 原因

领域层与 Qt 解耦后可在无 GUI 环境测试，也不会把 Widget、Signal 或 QImage 生命周期
引入项目文件和后台任务。显式 lifecycle controller 可以让所有破坏性入口共享相同
的未保存检查。

## 后果

- MainWindow 不直接替换或销毁 Document；
- P2 的图层命令和撤销必须通过 Document 的变更入口维护 revision/modified；
- P3 加入保存后，以保存成功和撤销 clean state 替换临时的放弃/取消限制。
