# V3 Eval Transparency & Smoke Gate Hardening

## 结论

Smoke Gate：PASS。仅执行 Tool Pipeline ON 一次；允许下一轮 ON/OFF 与轨迹重放，但本轮未执行这些实验。

增强复核：`runs/v3-hardened-smoke-20261008-r1/audit-report.json`。
原始结果：同目录 `report.json`、`tool_pipeline/ON/artifact.json`、`tool_pipeline/ON/provider-calls.json`；旧 V3 结果未覆盖。

## Eval 代理修复

- `v3_provider.py` 替代原 generator 代理。`responses.create` 即时调用原 SDK，不延迟创建请求；参数原样转发，事件与异常对象原样传播。
- `with_options` / `copy` 委托原客户端，并让作用域客户端共享观测账本。超时和 retry 配置不被 Eval 覆盖。
- 关闭操作直接进入底层 SDK Stream，包括其他线程发出的中断；不再调用正在运行的 Python generator.close。
- 请求输入复制为快照，避免 Context 后续修改历史记录。记录主请求、ContextRecord extraction、compaction 的独立类型、时间、状态、Usage 和失败事实。
- 不把 Usage 缺失记为零；聚合只提供可观测小计。观测写盘错误独立记录，不覆盖原 Provider 异常。
- 覆盖当前 Production 实际使用的同步接口，不宣称覆盖 SDK 的全部异步/扩展接口。每行表示 SDK create 调用，不是 SDK 内部每次 HTTP retry 的独立网络测量。

## 本次真实 Smoke

模型：gpt-6.1-sol，复用已通过的 Preflight，任务 prompt、正式工具 Handler、权限和 Runtime 配置保持不变。

| 指标 | 结果 |
| --- | --- |
| Agent terminal | completed |
| 主模型响应 | 11 |
| 每轮 Tool Call 数 | 3, 9, 5, 6, 6, 7, 6, 6, 1, 1, 0 |
| 多调用响应 | 8/11，72.73% |
| 成功工具调用 | 50；error 0；denied 0 |
| 实际并发批次 / 峰值 | 8 / 4 |
| 工具执行累计耗时 | 1289.20 ms |
| Agent 总耗时 | 442955.27 ms |
| Provider 调用累计耗时 | 430834.34 ms |

耗时来自带诊断探针的单次 Smoke，不构成性能提升或 ON/OFF 因果证据。

## 请求级 Usage

| 范围 | 请求数 | Usage 可得 | 不可得 | 可观测 input / output tokens |
| --- | --- | --- | --- | --- |
| Main Agent | 11 | 11 | 0 | 164228 / 6074 |
| Auxiliary | 1 | 0 | 1 | NOT_AVAILABLE / NOT_AVAILABLE |
| 全部 | 12 | 11 | 1 | 164228 / 6074，仅小计 |

唯一缺口：ContextRecord extraction 在约 8003.36 ms 被关闭，`status=CANCELLED`；作用域客户端 timeout=8.0、max_retries=0，durable Transcript 记录 `ModelCallInterrupted`。这与正式 extraction deadline 中断一致，不是已确认的 Provider 超时故障，更不是零消耗。未为此重复 Smoke 或追加重试。

## 三层 Oracle

| Oracle | 结果 | 验证范围 |
| --- | --- | --- |
| Tool Execution | PASS | 50 个 callId，名称/参数、单次 dispatch、成功 finished、dispatch 前 allow；无未观测额外派发 |
| Tool Result Delivery | PASS | 50/50：Finished 内联/外置内容、durable Context 输出与下一轮实际请求一致，无 Call/Result 错配 |
| Task Evidence | PASS | 必读源码全部读取，19/19 引用逐字匹配，不放宽省略号或去缩进引用 |

Task Evidence 是严格读取/引用结构判定，语义结论仍保留 `PENDING_MANUAL_REVIEW`；不把它包装为自动证明所有审计结论正确。增强 Oracle 从既有 Artifact 独立重投影，未重新执行任务，原始报告未改写。

## 验证与边界

- 新增针对性回归：13 项通过（代理/分层 Oracle/严格引用）。
- `.venv/bin/python -m unittest discover -s eval -t .`：117 tests，0 failures/errors/skipped。
- `.venv/bin/python -m unittest discover -s tests`：245 tests，0 failures/errors/skipped。
- `git diff --check`：PASS。
- 原目录与隔离源码快照哈希复核通过。Production Runtime / SDK 未修改；既有工作区改动保留。
- 本轮没有 OFF、其他主题、轨迹重放或新的 Benchmark 调优；没有 commit/tag。
