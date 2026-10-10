# Module Capability Benchmark V1（冻结规格）

本规格在运行新增 benchmark 前冻结。版本 modules-v1.0，代码和规格 SHA-256 随报告保存。
Production Runtime 不修改；FakeModel 不提供真实 token 成本或推理收益证据。

## Repository Evidence Matrix

| Module | Capability | Existing Evidence | Missing Evidence | Proposed Test |
|---|---|---|---|---|
| 1 Recovery | 4 类 crash、已完成结果补回、未知副作用保守恢复 | eval/phase3/scenarios.py 四个 fault injection；tests/test_session_recovery.py 的 checkpoint corruption/dispatch ledger/finished output replay | 稳定恢复延迟、真实进程 kill、外部 exactly-once 均未证明 | 原场景原 oracle 重跑并保存完整 pre/final transcript、恢复输出；单独执行 checkpoint 测试 |
| 2 Tool | permission-before-dispatch、结果配对、并发、异常、重试、大结果引用 | eval/phase2/scenarios.py + oracle.py；tests/test_tool_parallel_execution.py；tests/test_tool_failure_boundaries.py；tests/test_session_store.py | 固定 workload 串并行多次计时 | Production scheduler + Eval read-only handler；2/4/8 calls × 10/30ms，max_workers=2/4，8-call staggered case；warmup 1、paired repeats 7 |
| 3 Memory | explicit remember → disk → new Session recall；ContextRecord active/superseded/provenance | hexgent/tool/memory_tools.py；memory_store.py；tests/test_memory.py；tests/test_context_records.py；tests/test_context_record_extraction.py；eval/phase4 | 模型自主使用记忆、真实自动提取；Memory 撤销和语义替换 API 缺失 | FakeModel 调用 remember、新 Session 召回与 workspace 隔离、相同正文 metadata merge/无关噪声；复用 active/source 校验、压缩可访问性测试 |
| 4 Context | 安全轮次边界、活动 Plan、持久化 compaction、prompt_too_long 恢复 | eval/phase5/scenarios.py C1；eval/phase4；tests/test_context_compaction.py；tests/test_agent_loop_limits.py | 六类固定场景分别给出单次估算压缩率；广泛真实任务成功率 | 六个固定 durable context → production compactor → checkpoint restore；统一 estimator；补跑原 emergency recovery 测试 |
| 5 Audit | 六个 TaskSpecification、fixture clone/reset、外部 oracle、per-call、revision、usage | eval/phase5c/test_feasibility.py / test_report_correction.py；corrected Batch 1 | 旧 artifact 缺参数、输出、Transcript；12 候选任务清单身份尚需定位核实 | 复用 Phase5C tests，核验 corrected artifact，字段覆盖以冻结必要字段为分母 |

## 外部证据与限制

- Phase5B 原始 JSON：eval/phase5/runs/real-model-phase5b-20261007/phase5b-real-model-report.json。重新从文件计算输入 token/延迟 delta，不采信任务文字中的百分比。该 artifact baseline usage 完整 5 次、enabled 完整 4 次，聚合均值差只能作为原报告算术核对，不能视为完整 paired 成本收益。预置 Memory/ContextRecord 只证明 fixture ON/OFF 行为，不能证明自动提取。
- Phase5C 原始与 corrected 报告：batch1-20261008T071913Z 及 corrected-r1。Provider 故障独立统计；D2 有契约欠明确证据；旧命令与输出无法重建。
- Memory content-based identity：同正文 upsert 合并 keywords；不同正文产生新 item。没有正式撤销接口，因此 stale_memory_recall_rate 不适用，语义替换能力记录 BLOCKER。
- ContextRecord provenance 校验证明提交时 source/quote 有效，不等于任意外部文件变化自动撤销记录；该能力 UNVERIFIED。
- 本地 workspace permission 并非 OS sandbox。

## 冻结测量契约

1. Recovery：四类窗口各 1 case，先执行已有 oracle。再核对 transcript sequence 单调唯一、重复 restore output 相同且每 callId 一个 output、restore 无新增 dispatch。checkpoint 单列实际测试数，不把 4 项 transcript 检查冒充 checkpoint 场景。恢复耗时不报告稳定性能结论。
2. Parallel：固定 FakeModel 输出一次 tool trace，串并行使用相同 calls、read-only handler/内容。计时从开始消费 scheduler generator 到 StopIteration 收集完整 batch（包含 permission/preparation/commit，不含 Provider）；每 case 先 warmup 后交替 AB/BA 7 次。保存每次 wall time、输出及 pairing、实际完成顺序/peak concurrency；nearest-rank P95 仅小样本描述。工具 delay 是 fixture workload，不是实际生产工具性能。
3. Safety：复用 Phase2 六类实际场景与现有 timeout/out-of-order/large result/duplicate tests。逐 call 根据 durable ledger 计算 unauthorized dispatch、结果错配和重复 dispatch；拒绝本身不是失败。测试方法数与场景数分别报告。
4. Memory：3 个不同约束案例，用户显式请求 → FakeModel remember → disk → 新 Session；recall@4 是 required memory identity 的命中比例，fact_accuracy 只检查内容精确一致。新 Session 只问约定，不重述事实；保存正式 Agent Loop 请求证明实际注入。噪声作为同工作区普通 project item；跨工作区不得泄漏。ContextRecord 另设 1 个固定候选场景：正式 extraction/source validation → 显式 supersede → compaction → restore → recall/format injection。production/recall/retention/continuation 指标分母为该 1 项机制 fixture，绝不是模型自主提取或任务收益；外部来源自动失效没有正式契约。
5. Compaction：long_history / multi_read / large_result / active_plan / near_budget / complete_rounds 六类；固定摘要保留事实 FACT=ALPHA。统一 Production estimate_request_tokens 计算完整输入前后差异，ratio=(before-after)/before。保存原始/恢复后的上下文、摘要请求及 durable transcript。检查完整 call/output 同组、最新 Query、Plan 不变、compaction fact 已提交且 restore 一致。固定摘要事实保留不是模型自主摘要能力。
6. Audit：TaskSpecification 六项为 implemented candidates；更大候选清单数若无法定位为 UNVERIFIED，不直接声称 12 qualified。qualified/completed/oracle-pass 按 task identity 去重，Provider 未执行完不当作 task design failure。D2 资格沿用冻结历史判断，同时保留 contract caveat。audit_field_coverage 仅针对本批旧 artifact 的明确字段集合，不外推所有 run。

## 输出与执行

python -m eval.modules_v1.runner --output-dir eval/modules_v1/runs/<unique-id>

每模块记录 specification、fixture/scenario、oracle、raw artifact、metrics、limitations、resume claim；metric sources 指向 raw artifact。已有单测通过 unittest 执行并保存实际 test ID/结果，不按实现存在计覆盖。
所有新调用 client 均为 FakeModel；入口额外禁止 OpenAI client HTTP transport 发请求。Production 与冻结 TaskSpecification/Oracle/fixture 的输入文件 hash 前后相等。

真实模型最小待验：explicit remember 新 Session 使用约束的外部 oracle；真实 StageA extraction→qualified ContextRecord→StageB；固定六类 compaction 后真实任务 oracle。均 PENDING_REAL_MODEL_VALIDATION，本轮不执行。
