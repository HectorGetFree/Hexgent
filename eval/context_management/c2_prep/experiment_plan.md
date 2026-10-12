# C2 真实实验审批计划（尚未执行）

12 个固定样本，每样本一次 Production Agent Run，真实模型仅用于 COMPACTION；压缩后的 MAIN 用 Fake 终态定位实际请求，不评测模型行为。使用同一 Provider/model/参数与 Production 摘要 Prompt。拟沿用 gpt-6.1-sol，但实际 Provider、计价、模型上下文窗口需审批前核对。

固定事实、内容哈希和 Oracle 不因模型结果改变。不重新生成至 PASS。摘要错误、缺失/错误事实、未获接受均保留；普通质量失败继续后续样本，隔离/审计/成本/预算故障立即停止。

本轮每样本约 3 个完整历史轮次；较低 proactive 阈值用于可重复触发真实 Production 路径，不代表默认 64000 Token 的长历史压力测试。真实调用前需审批是否采用该规模；没有人工许可不得扩写样本或调整触发参数。

## 调用上界

期望 12 次真实摘要调用。每次 Compaction 最多 2 attempts，允许一次 partial 后再一次 operation，保守上界 12 × 2 × 2 = 48 次逻辑调用。必须关闭 SDK 自动重试；否则还可能乘以 SDK Retry，不是明确 HTTP 上界。MAIN 使用 Fake，不计真实调用。

输出参数上限 2000/call，因此参数层输出上界 96000 tokens。输入真实 Token 上界尚未由模型/Provider 确认，本地估算不能冒充上界。

费用上界公式：48 × (I_max × p_input + 2000 × p_output)，需换算计价单位并按缓存/失败计费调整。当前 I_max、价格和失败计费 UNKNOWN，因此金额上界 NOT_AVAILABLE，不批准付费执行。审批前确认这些参数及金额预算，逐调用按最大可计费额度预留；余量不足停止派发。Usage 缺失停止成本实验，不重试掩盖缺失。

当前 Gate：BLOCKED_FOR_C2。补齐明确费用上界及请求归属 Gate 后再申请 READY_FOR_C2_REAL_MODEL_REVIEW。不得自动进入 C2/C3。
