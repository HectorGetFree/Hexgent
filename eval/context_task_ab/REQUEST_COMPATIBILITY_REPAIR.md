# reasoning.status 请求兼容修复

## 影响与根因
IS1真实Smoke第4次MAIN请求收到HTTP 400：Unknown parameter: input[1].status。错误发生在真实主模型请求，阻断工具后续执行，不属于压缩失败或任务推理失败。
ContextManager.model_messages→materialize_messages→_request_message把SDK响应对象完整model_dump为请求条目，回传了reasoning.status。当前SDK输入类型允许省略该字段，但现有Provider拒绝此次回传；不将此行为泛化为所有Responses Provider的要求。

## 最小修复
仅修改hexgent/context/context_manager.py的请求副本物化：顶层reasoning条目省略status；保留id、summary、encrypted_content及其他内容。非reasoning条目、嵌套业务字段、Call ID及Tool Result状态不变。
原始活动历史、Transcript、Checkpoint不改写。正常请求、摘要/Extractor历史物化以及正式Restore后请求复用同一边界。原有logprobs=null→[]兼容行为保留。

## 离线证据
- 新增5项测试：空/非空reasoning状态、SDK与恢复JSON一致、其他状态/工具配对保留、真实文件工具下一轮请求、正式Restore→Continue请求。
- 与原logprobs回归合计9/9通过。
- Runtime全量355/355通过；IS1任务Gate及观测代理10/10通过；git diff --check通过。
- 原始失败请求离线重物化：reasoning.status数量1→0；其他条目完全不变。没有Provider调用。

历史失败Artifact保留，未重跑真实模型。此结果证明请求修正及工程回归，不证明当前Provider已真实接受完整修复后请求，也不能排除其他Provider字段兼容性问题。后续真实复验需另行执行。
