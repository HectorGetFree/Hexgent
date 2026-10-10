# Tool Real-Workload V2 — 待人工审核

状态：**PENDING_HUMAN_REVIEW**。仅完成静态扫描与计划构建，未运行任何正式 V2 Benchmark。

机器可读计划：`V2_SOURCE_MANIFEST.json`。每个调用都有稳定 call_id、正式工具名、完整 arguments、读取目的、源文件 SHA-256、字节/行数和顶层符号。`v2_manifest.py` 只用静态文件扫描生成计划，不调用 Tool Pipeline。

## 覆盖与规模

| 任务 | Discovery / Core / Dependency / Tests 调用数 | 总调用 | 唯一读取文件 | 实际读取字节（含测试） |
|---|---|---:|---:|---:|
| Tool Pipeline | 4 / 4 / 12 / 6 | 26 | 22 | 270095 |
| Recovery / Checkpoint | 4 / 5 / 11 / 5 | 25 | 21 | 345347 |
| Memory / Context | 4 / 6 / 14 / 7 | 31 | 27 | 278092 |

各任务均为四批，Discovery 为四次 `glob(pattern)`，其余为 `read_file(path)` 全文读取。任务内 duplicate_reads=0。不同任务可覆盖同一关键文件，这是不同审计问题的合理重叠，不能跨任务相加宣称唯一源码覆盖数。

字节数是静态文件原始字节，不是工具输出大小、模型 Token 或 Agent 阅读理解质量。JSON 将 implementation/source bytes 与 test bytes 分开。完整读取使缓存冷启动下内容 Oracle 可使用源码原始正文，不人为切片增加调用数。

## 1. Tool Pipeline

核心：`tool_registry.py`、`tool_scheduler.py`、`tool_execution.py`、`builtin_tools.py`。

覆盖声明 → 参数校验/权限 → prepare/dispatch → retry → 并发分组 → 顺序提交/外置结果。追踪 `agent_turn.py` 主要调用方、`tool_context.py` 缓存指纹与锁、`tool_result.py` 的结果/失败结构、`tool_retry.py`、`tool_permission_pipeline.py`、`tool_validation.py`、`session_store.py`、`config.py`、`run_control.py`、`events.py`、`prompt.py` 和 `token_estimator.py`。

六个测试文件：tool_parallel_execution、tool_failure_boundaries、tool_deduplication、permission_pipeline、tool_permission、session_store。

边界目标：并发上限与串行屏障、权限先于派发、结果 Schema、失败/重试、未知副作用、取消/超时、输出外置。排除每个 Workspace/Skill/Plan Handler 的业务实现，不声称全部 Tool 业务覆盖。

## 2. Recovery / Checkpoint

核心：`session_restore.py`、`checkpoint.py`、`tool_call_recovery.py`、`session_store.py`、`session.py`。

覆盖 Transcript/Checkpoint → 校验/选择 → replay → 未闭合调用分类 → Context/Plan/RunState 重建 → continue 入口。追踪 `agent_engine.py`、`loop_state.py`、`agent_loop.py`、`agent_turn.py`、`tool_scheduler.py`、`context_manager.py`、`plan_model.py`、`config.py`、`run_manager.py`、`runtime_adapter.py`、`run_control.py`。

五个测试文件：session_recovery、session_store、loop_interrupt、runtime_command_effect_reconciliation_phase4、agent_engine_stream。

边界目标：损坏快照 fallback、安全快照、finished 缺少 Context output、未知副作用、恢复入口状态与命令 reconcile。这里只读源码，不重新运行 Recovery 故障注入；不展开全部 HTTP/CLI 与 Lifecycle 投影实现。

## 3. Memory / Context

核心：`memory_store.py`、`memory_retrieval.py`、`context_records.py`、`context_retrieval.py`、`context_manager.py`、`context_builder.py`。

覆盖 remember → Workspace Memory 存储/召回；Session ContextRecord → extraction/source validation → durable replay/retrieval → 请求组装；完整轮次压缩与预算。追踪 `context_record_extraction.py`、`memory_auto_extraction.py`、`memory_prompts.py`、`context_compaction.py`、`context_budget.py`、`memory_tools.py`、`extract_context_records.py`、`agent_turn.py`、`agent_engine.py`、`session.py`、`session_restore.py`、`config.py`、`retrieval_text.py`、`token_estimator.py`。

七个测试文件：memory、context_records、context_retrieval、context_record_extraction、context_compaction、session_recovery、agent_loop_limits。

边界目标：Workspace 与 Session 作用域、active/superseded、来源与 quote、失败提交、恢复、预算、无效摘要与完整工具轮次。读取 auto extraction 模块是确认实际实现/接入边界，不预设已接入 Agent Loop。不测模型理解或记忆行为收益。

## 批次与依赖

1. Discovery：目录存在性和候选文件名确认。
2. Core Reading：主要职责、核心接口和数据结构。
3. Dependency Tracing：基于准备阶段已确认的核心调用链，读取配置/调用方/状态契约。
4. Test Verification：按上述边界读取测试的实际断言。

完整计划在测量前冻结，不由工具返回动态选择后续路径。Discovery 的 expected_paths 已保存。正式运行时先核验发现结果符合计划再进入 Core；任意文件读取失败或错误状态必须终止任务并记为无效结果。

三个跨批次屏障保留，不能将 26/25/31 次调用摊平成单批。每批不同文件只读、参数已经冻结，可安全并发；文件之间存在语义引用，不构成读取动作间的数据依赖。这是确定性审计工具轨迹，不是模型自主推理任务。

## 审核后执行协议（本轮不执行）

- Serial：正式 Pipeline enabled=False；Parallel：正式默认 enabled=True/max_workers=4。
- 每任务预热一对、至少二十对测量，交替先后顺序；每个 variant 独立 ToolExecutionContext，任务内跨批保留同一 Context。
- 建议使用独立临时 durable Session，保留正式输出预算/外置路径；Session 创建不计时，工具事实提交、输出外置计入批次。与 V1 的无 Session batch timing 明确不同，不直接做倍数对比。
- 完整任务时钟覆盖四个顺序批次及必要结果核验/推进，另保存四个 batch timing。不得混入 FakeModel API latency；这是完整工具序列，不是完整 Agent 任务延迟。
- 每个计时样本保存 latency、批次贡献、Call ID/name/arguments/status、完整输出或可解析外置正文的 hash、输出顺序和最终结果集合。无错误响应等价冒充成功。
- Artifact 路径、preview 等表示不直接比较绝对字符串：按 callId 解析 durable Artifact，保存全文 hash 与引用一致性；不能忽略正文差异。
- 每对前后核对全部读取文件 SHA-256、Discovery 返回集合及正式工具相关代码/配置；测量中变更记录 INVALID_SOURCE_CHANGED，保留原样本并停止，不默默筛掉。准备清单与源码不一致时不得自动刷新清单后继续。
- 所有计时样本保留；P95 nearest-rank ceil(.95*n)。顺序本机重复不是独立冷 I/O 样本。
- 并发峰值继续用单独非计时 profiler pass 记录，明确不是每个计时样本的峰值；若要求逐样本峰值，需单列 instrumented 运行，不能与 uninstrumented latency 混合。

## 分析边界

- 调用增多是否摊薄开销：看每批 call count、大小和 latency contribution，但不能单凭 V1/V2 差异作因果断言（文件及持久化范围不同）。
- 文件大小：按本次真实文件分布作描述性分析，不复制文件/人为放大内容；三任务不足以建立稳定大小—加速因果模型。
- glob 与 read_file：分别比较 Discovery 与读取批次；本计划没有混合工具批次，**不能声称已隔离验证混合执行的因果影响**。不为了补此指标添加无意义调用。
- 屏障：报告每批与任务级加速及关键耗时贡献；四阶段不可重叠，对总收益有天然限制。
- 热缓存：不清 OS cache，不增加 sleep，不能由耗时单独断言 I/O 是瓶颈；线程池/GIL/持久化开销仅列待验证解释。

审核前停止。V1 与 Module V1 结果原样保留；本轮无 Production 修改、无测试/Benchmark 执行、无 commit/tag。
