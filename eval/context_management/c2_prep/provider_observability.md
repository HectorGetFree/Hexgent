# Provider 观测准备

Production Compaction 是独立 Responses 调用，与 MAIN 共用配置客户端和模型。`hexgent/context/context_compaction.py` 保存成功转换估算；`hexgent/observation.py` 按 span 保存 MAIN_AGENT/COMPACTION 与 attempt。`hexgent/run_report.py` 的累计 Usage 不能替代完整 Compaction 费用账本，失败、拒绝摘要成本需额外归集。

Eval 复用 `eval/tool_hardening/v3_provider.py` 的透明 Observer：保存完整请求、终态响应、原始 Usage、请求类型、耗时及失败原因，with_options/copy 共享账本，异常及流关闭向底层传播。C2 OfflineObserver 仅适配 Fake 对象序列化，不修改 Runtime。

本轮 12 次 COMPACTION + 12 次 MAIN 的完整参数及响应已保存。每条附 fixture/session/call ID；Production Transcript 和 Session 内 observation.jsonl 同时保留。真实 Usage 为 NOT_APPLICABLE。

尚未完成：逐调用 execution_id/span_id/attempt 的显式一对一归属与缺失 span 拒绝 Gate；完整原始 stream 事件只保留类型和终态响应，非全部事件正文；重试/失败/取消场景的 C2 专用归属测试。不能声称成本观测 Gate 已完整通过。后续最小 Eval 补充应读取 current_observation，关联 provider_started/finished，检测未配对 span，保存全部流事件，并在 Usage 缺失时阻止费用结论；不需修改 Production。

Provider 隐藏 SDK Retry 必须以 Eval client.with_options(max_retries=0) 关闭并留证；否则 SDK create 一次不等于实际 HTTP 一次。Provider 内部重试/失败计费仍需服务方确认。不得因失败调用未提供 Usage 就记为零。
