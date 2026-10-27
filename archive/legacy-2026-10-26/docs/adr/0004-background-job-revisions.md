# ADR-0004：后台任务的 revision 提交规则

- 状态：Accepted
- 日期：2026-07-23

## 决策

主线程独占可变 Document、Widget、QImage 缓存和撤销栈。后台任务只接收不可变
快照、复制的 Tile 或文件路径，并携带任务开始时的 Document/Layer revision。

Worker 返回纯 `OperationResult`。主线程提交前重新校验 revision：一致时通过
Command 应用；不一致时丢弃结果并提示操作已过期。长任务必须支持取消令牌。

## 原因

让 Worker 直接修改活文档会产生竞争、过期结果覆盖新编辑和 Qt 线程亲和性问题。

## 后果

- 后台线程不得直接触碰 UI 或 QUndoStack；
- 所有后台结果都有成功、取消、失败和过期四种终态；
- P0 只冻结规则，具体 JobRunner 在后续阶段实现。
