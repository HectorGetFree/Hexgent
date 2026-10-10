# Recovery Evidence Hardening V1

仅新增 Eval；不修改 Runtime、策略、Phase5C 或原 Artifact。

## 故障模型

子进程 A 使用 Production AgentEngine/Agent Loop/Permission/Tool scheduler，注册 Eval 审计工具及参数 Schema。
在目标位置调用 os._exit(73)，不执行 Python unwind/finally。父进程确认 A 退出后才启动 B。
B 只读取 workspace 下的实际 Session；先尝试 RunRequest.continue_run()。不支持时记录 BLOCKED，再独立检查 RunRequest.submit() 的 Session 上下文延续；后者绝不计作原 Run 续跑成功。
工具和 FakeModel 是确定性 fixture；禁止真实 HTTP。工作区是可信临时 fixture，不是 OS sandbox。

## 固定场景

1. finished_before_context：finished 已追加、tool_results context 尚未追加。
2. safe_started_before_dispatch：只读工具 started 已追加、尚未派发。
3. handler_before_effect：真实 handler_entered 后、副作用前退出。
4. handler_partial_effect：写入 PARTIAL 后、提交前退出。
5. committed_before_finished：写入 COMMITTED 且 handler_returned，finished 前退出。
6. permission_denied：真实用户 deny 已追加、finished 前退出。
7. permission_pending：通过 Production RunManager 注册 PendingPermission、写入 permission_requested(PENDING) 后退出，不直接伪造权限事实。
8. safe_checkpoint：正式 tool_batch_completed Checkpoint 提交后退出，作为同一 Run continue 的正向对照。

## 外部账本与 Oracle

Eval 工具在独立 JSONL 写 handler_entered/side_effect_started/side_effect_committed/handler_returned，每次 flush+fsync；业务对象保存 PARTIAL 或 COMMITTED 并 fsync。
这仅加强 Eval 观测，不改变 Runtime 的持久性承诺，不模拟断电。
父进程检查 A/B PID、退出码，逐 call durable finished/output 对应、恢复 output 精确内容、模型请求 input、最终 Agent 状态、外部账本逐字节不变、业务状态和 dispatch/permission facts。
无 finished 的副作用调用必须明确 unknown/verify_before_retry，既不自动补提交也不自动重试。
DENIED/PENDING 不得出现任何 dispatch、handler 进入或后续 allow。PENDING 请求的重新开放/HTTP resolution 不在本轮证明范围。
恢复输出补回最多一次，第二次恢复在不同子进程中的 Engine 实例执行，但 B 进程内两次 restore 不作为并发恢复证明。

## 报告语义

分别列 Existing Protocol Recovery Evidence / New Cross-Process Resume Evidence / New Side-Effect Safety Evidence / New Permission Recovery Evidence。
同一 Run resume、Session follow-up、side effect safety、permission 各有独立结果。存在有效接口阻塞时整体 PARTIAL_BLOCKED，不能因其他 Oracle PASS 宣称全部 resume PASS。
保留原四窗口和九测试的历史结果与原始 Hash，不重新运行或重解释它们。
不宣称外部 exactly-once、掉电安全、任意模型行为、并发 restore 或恢复修复自身再次崩溃安全。
