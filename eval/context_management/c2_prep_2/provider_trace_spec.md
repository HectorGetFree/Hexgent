# Gate A：调用归属契约

入口：Production `context_compaction.compact_context_if_needed → client.responses.create`；MAIN 为 `call_model.call_model → client.responses.create`。Eval `Trace.responses.create` 包装同一 SDK 调用边界，不替换 Builder、摘要调用、权限与工具行为。

每条调用保存 eval_run_id、fixture_id、实际 Session ID、Session 当前 internal run_id、RunObservation execution_id、真实 provider_started span_id、attempt、派生 retry_index、provider_call_id、类型、hostname、model、请求 hash/正文、全部流事件、终态响应、Usage 和耗时。provider_call_id 是 Eval 审计标识，不冒充 Provider request ID；compaction_attempt_id 复用真实 Span ID，不伪造 SDK 的重试 ID。

current_observation 在正式 AgentEngine.run 内提供关联，紧贴 create 读取最新 provider_started；单 Run 的 Provider 同步串行调用是本方案前提。多个 Run 共用客户端时必须使用独立 Trace 绑定并共享 Budget，不得共享错误的 Session 绑定。

最终与 Production observation.jsonl 一对一复核：每个已派发调用需唯一 started/finished，类型一致。预算拒绝产生的未派发 Span 单列，不增加 Provider 调用数；其他无解释 Span 阻断。Runtime 没有暴露可获取的字段保持 NOT_AVAILABLE，不能生成看似真实的 ID。

一次流终止只提交一次账本；返回Usage原始缓存字段保留，缺失input/output为null/MISSING。MAIN/COMPACTION各自汇总，任一类缺失则该类总量null，可观测小计需另行标注。Fake协议usage不是实际费用或Token。

SDK物理请求边界：实时profile强制max_retries=0；离线 OpenAI SDK + httpx.MockTransport 验证HTTP500仅触发一次transport请求。with_options/copy转发与共享账本，不开启SDK重试。这里只验证SDK客户端边界，不保证RightAPI网关没有内部重试或重复计费。网关计费需单独确认。

测试覆盖正常15样本、格式拒绝后重试成功、Provider异常后成功、超时后成功、Usage缺失、关闭流取消、MAIN/COMPACTION并存、Production预算拒绝零派发及Eval额度拒绝。见test_results.txt和各run的provider-trace.json。
