# 根因与修复范围

旧链路：Production Loop → Compaction begin_span → TokenTrace → TraceResponses 从日志选择最后一个 provider_started → TokenBudget.reserve → GuardedResponses 读取 calls[-1] → SDK。

两个校验缺口：未验证 Span 是否仍然有效；授权行并非当前请求显式绑定。reconcile 事后发现重复 Span，但不能撤销已经派发的请求。原复现两次调用各自预留额度，因此不能把归因缺陷表述为已证明的 Token 额度绕过。

修复独立放在 guard.py、entry.py，旧 control.py、budget.py 和冻结 Artifact 不改。

新链路：Production begin_span 原样执行 → Eval ContextVar 绑定该真实句柄 → 共享锁验证 Session / Observer / Execution / Span 生命周期 / 请求类型 / 逻辑 Attempt → 独立预算预留 → Call 与 Reservation、Eval Provider Attempt、请求哈希显式绑定 → 派发前再次检查并一次性消费 → SDK。不再读取最后一条日志或 calls[-1] 作为许可。

同一共享锁管理 Span 状态、Attempt 占用、预算预留、Reservation 消费。Provider I/O 在锁外，消费点为派发授权线性化点，不声称对已经授权或发送的请求进行撤回。持久化失败拒绝派发，并保守保留已预留额度。

当前 Production Compaction 在每个逻辑 Attempt 内新建 Span，失败后结束旧 Span，再创建新 Span；不是同一 Span 隐式重试。该事实与任务中“可能同 Span Retry”的假设不同。正常路径原样保留。另支持显式 Eval new_physical_attempt：要求上一物理调用已失败且 Span 仍有效，产生独立 Call / Reservation / Eval Attempt ID；它不伪称 Production 自带的字段，也不用于 Runner 主动重试。

真实 SDK 默认禁用，必须 --execute；SDK max_retries=0，Eval retries=0。MAIN 始终 Fake。Mock 路径标记 is_real_compaction_call=True 表示走真实 SDK 入口，但 transport_mode=MOCK_TRANSPORT、actual_network_dispatch=False；不是已执行真实 Provider 请求。外部无法据此宣称路由可用性、实际 Usage 或真实摘要质量。

Eval 仅包裹 Production 观测 begin/end 的导入入口，仍调用原函数；未替换 Agent Loop、Context Builder、Compaction 业务判断、摘要 Prompt 或 Handler。模块级插桩不支持在同进程并发运行多个独立 Benchmark Runner；单 Guard 内跨线程交错有专项测试。
