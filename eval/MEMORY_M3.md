# M3 ContextRecord Production Audit

审计日期：2026-10-10。READ-ONLY AUDIT + EVAL DESIGN；本轮不执行测试或真实模型，不修改 Production，不覆盖 M1/M2。
代码位置以下均相对于 `/Users/hector/dev/hexgent`。测试证据表示阅读到了相应断言，不表示本轮重跑 PASS。

## 1. 结论与定位

ContextRecord 已具备 Session 内持久化、增量模型提取、显式替代、受预算召回和 Transcript 重放。
但现有链路不能直接支持“单个 Run 自动及时更新所有关键事实、并在恢复后保证旧事实不注入”的完整承诺。

三个重要差异：

1. Session ≠ Run：新 submit、当前 Run 的 steering 会召回；普通工具轮次后的 extraction 更新 Session Store，但不刷新 `LoopState.relevant_context`。同 Session 的下一次 submit 可以使用新记录，同 Run 的下一轮未必能使用它。
2. 自动 Supersede 当前依赖用户更正来源；单靠成功工具结果 B 替代 A 不符合 parser 的证据规则。直接 Engine upsert 又是更弱的受信调用入口，不能混写成模型自动替代。
3. Restore 重建的 Store 与 Checkpoint 的已召回快照是两套状态；Continue 没有 ContextRecord 权威复核。存在陈旧快照注入风险，尚未用本轮运行验证。

不能照搬 M2：ContextRecord 没有其墓碑、Invalidate/Delete、请求前撤销锁或当前权威复核保证。

## 2. 概念与数据模型

| 对象 | 当前职责 / 权威 |
| --- | --- |
| Transcript | 持久化事件事实；Record mutation/提取提交的重放权威，不保证事实内容本身正确 |
| ContextRecord | 当前 Session 的简短 constraint/fact/decision；来源指向 Transcript 序号 |
| Long-Term Memory | 工作目录 Scope 的跨 Session 记忆；M2 独立 lifecycle.json 权威 |
| Checkpoint | 运行预算、状态、已召回条目的快照；不是 ContextRecord 最新权威 |
| Compaction | 压缩活动历史消息；不删除完整 Transcript，不负责 Record 生命周期管理 |

`hexgent/memory/context_records.py:22–74,137–154,178–254`：

- Input：`content, category, source_sequences, supersedes`。
- Record：`record_id, content, category, source_sequences, status`，无时间戳、置信度、来源类型字段、版本号、superseded_by。
- ID：`ctx-` + SHA-256(category + 换行 + 规范化正文) 前 12 位；不同类别/正文生成不同 ID。
- content：规范化空白，1–500 字符；source_sequences 必须非空正整数，去重排序。
- 状态仅 `active/superseded`；没有删除、单独 invalidated、TTL、文件 revision/hash。
- 同类别同正文合并证据；`upsert` 显式将 existing.status 设为 active，意味着旧正文重复提交可以重新激活已 superseded 条目（99–128）。没有自动语义冲突识别。
- Supersede 引用必须存在；旧 active 改 superseded；重复替代跳过；替代关系可从提交事件推断，Record 本身没有后继链接。
- projection schema_version=2 是文件协议版本，不是每条事实的修订版本。

## 3. 实际写入链路与来源边界

### 自动路径

`AgentEngine.run → agent_loop → run_turn → 完整轮次 durable facts → extract_context_records → prepare_extraction → 辅助模型 → parse_candidates → Session.upsert_context_records(processed_sequence=...)`。

证据：`hexgent/loop/agent_loop.py:63–110`、`hexgent/loop/extract_context_records.py:15–96`、`hexgent/memory/context_record_extraction.py:11–140`。

- 默认 enabled，积累 4 个完整 context_messages_appended 边界；force=False。不是每个工具调用后提取，也不保证短任务一定产生记录。
- 仅已完整回填的轮次之前的 Transcript 被展示。输入包括用户、模型消息、工具 finished，但可用证据更严格。
- 默认事实预览单事件 1,000 字符、总预算 8,000 字符、候选最多 4、已有背景最多 4、输出 1,000 Tokens、提取超时 8 秒；以实际加载配置为准。
- 大工具输出可能被截断；外置结果文件不会因此自动全文读取。批次处理进度可越过已截断事件，尾部信息不能假定会在下批补全。
- fact 可引用用户输入或 success 工具 finished；constraint/decision 或任意 supersedes 必须引用用户输入。模型自述不能单独充当合法证据。
- `evidence_quote` 必须是至少一条允许来源预览的非空子串；**没有验证 content 被 quote 蕴含，也没有要求 quote 覆盖所有 source_sequences**。同一句真 quote 可被配上错误 content。
- quote/source-type 不写入 ContextRecord；持久化后只有序号，审计需要回看 Transcript。
- “用户表示”、敏感信息过滤、用户更正确实授权替代等要求主要在 `hexgent/memory/memory_prompts.py:4–14`；不是全部有硬校验。不能称为完整可信度系统。
- 记录和进度合并为一个 `context_record_extraction_committed` 事件；日志提交前不改内存；投影失败可重建。超时/解析/提交错误在 extraction 入口被捕获，同 Run 抑制再次提取，start_run 重置；不能因此认定本次记录已经保留。
- 缺失 Usage 在 extraction_response 的聚合字段可能记为 0；评测应以 Provider observation 原始 null 为准，不能把 0 当实测。

### 受信 API 路径

`AgentEngine.upsert_context_records → Session.upsert_context_records → staged Store → Transcript mutations → projection`。
证据：`hexgent/agent_engine.py:368–379`、`hexgent/session/session.py:397–468`。

Engine 禁止活跃 Run 内外部插入；Session 仅检查来源序号小于 next_sequence，没有自动 parser 的 quote/来源类型校验。来源只“已落盘”，不等于“真正支持正文”。未发现模型注册 ContextRecord 管理工具；不能把该 API 当作自动提取证据。
手动多 mutation 分条记录，若中途落盘失败可能部分提交而内存 staged 尚未替换；自动单事件提交的原子性不能推广到手动批次。属于代码推导风险，缺少对应失败注入证据。

## 4. 检索、注入、去重

`submit → Session.retrieve_context_records(active_records) → ContextRecordRetriever → LoopState.relevant_context → build_context_details → relevant-session-context → 主 Provider 请求`。
Steering 在 `hexgent/loop/agent_turn.py:243–265` 重新召回；普通 extraction 不重新赋值 relevant_context。
证据：`hexgent/agent_engine.py:307–343`、`hexgent/session/session.py:468–501`、`hexgent/memory/context_retrieval.py:102–207`、`hexgent/context/context_builder.py:102–169`。

- 默认最多 6 条/2,000 正文字符，阈值 0.15；无词法匹配的 constraint 最多 3；指代 query 可启用最近来源回退。排序为词法分数、较新 source sequence、ID。
- Session 入口传 active_records，但 Retriever 本身不检查 status。不能把直接传任意 records 的调用也当安全入口。
- 不验证源文件当前状态/来源正文仍真实、成功工具是否仍有权威性。外部文件变化不会自动 invalidation。
- 不去重当前 active_messages/summary 中已有的事实；Record 注入开销可能增加。
- 与 Memory 仅做 casefold/空白规范化后的正文精确去重，Record 优先，统一 Token 预算裁剪；不同表述/冲突值仍可能一起注入。冲突优先级是给模型的文字提示，不是事实融合器。
- Memory 去重发生在统一预算裁剪前；被剪掉的 Record 也可能已使对应 Memory 被移除，不能保证事实最终至少从一个渠道进入请求。
- `context_records_retrieved` 记录 ID/评分/原因，不证明裁剪后真正发送；BuiltContext 不产生 ContextRecord 专用最终 receipt。需要 Eval 透明 SDK 参数记录及 span 关联，不能用检索事件替代实际请求证据。

## 5. Compaction 与恢复

Compaction：`run_turn → build_context_details → compact_context_if_needed → context_compaction_applied`，代码 `hexgent/loop/agent_turn.py:305–348`、`hexgent/context/context_compaction.py:153–242`。
仅压缩活动消息，按完整轮次切点，记录库不随压缩删除；摘要模型输入是历史消息，不是当前有效 Record 的权威列表。摘要格式/缩减收益有校验，旧事实不得复活没有机器语义校验。摘要可能保留旧值；不应把摘要中的旧内容全部计算成 Record 渠道泄漏。

Restore：`SessionRestorer.restore` 从完整 Transcript 重放全部 Record mutations/提取游标，与 Compaction 重放独立；`RunStateSnapshot` 同时保存 relevant_context 的 ID/正文/来源/score。
证据：`hexgent/session/session_restore.py:140–168,277–303`、`hexgent/loop/loop_state.py:39–100,250–266`。

- JSON 投影删除后可重建；完整 Transcript 仍保留来源，不等于源文件仍新鲜。
- Continue 继承 snapshot.relevant_context，`AgentEngine._build_run_state` 仅重新校验 relevant_memory（327–343），Builder 不接 Session Record Store。
- `_pending_run_state` 的后续 stateful_events 不含 Record changed/extraction committed。Checkpoint 后 Engine 安全 upsert 旧 A→B，恢复 Store 可为 B，而可续跑快照仍为 A。**静态可达陈旧注入路径；尚未做 M3 复现，不宣布已测 FAIL。**
- 被 superseded 正文重新 upsert 设 active 是当前规则，不能假设存在 M2 式永久撤销。

## 6. 能力与测试矩阵

| 能力 | 分类 | 实际证据 / 缺口 |
| --- | --- | --- |
| 创建/精确合并/显式替代/投影重放 | IMPLEMENTED_AND_TESTED | tests/test_context_records.py::test_upsert_persists_projection_and_replays_from_transcript |
| 来源序号未来值拒绝、Engine 安全入口 | IMPLEMENTED_AND_TESTED | 同类 test_rejects_source_sequence_that_is_not_yet_a_transcript_fact / test_engine_exposes_session_record_submission_at_safe_boundary；不证明来源语义 |
| 自动 extraction 频率/安全尾部/预算/解析 | IMPLEMENTED_AND_TESTED | tests/test_context_record_extraction.py::test_interval_budget_and_unclosed_tail / test_source_and_quote_validation |
| 自动提交游标与记录、失败隔离 | IMPLEMENTED_AND_TESTED | 同类 test_commit_and_empty_commit_restore_together / test_failed_commit_does_not_mutate_memory / test_invalid_output_and_interrupt_preserve_progress / test_failed_extraction_is_suppressed_until_next_run |
| 新 Query 召回、约束回退、预算、Builder 注入 | IMPLEMENTED_AND_TESTED | tests/test_context_retrieval.py 四个方法；Engine 主 Loop 在注入测试被 patch，不等于真实模型正确使用 |
| 替代旧记录不被新 Query 召回 | IMPLEMENTED_AND_TESTED | eval/phase4/scenarios.py::superseded_context_record_is_not_recalled；eval/phase5/scenarios.py::_run_context_record_variant，均受信预置/FakeModel |
| 自动工具观察→记录→真实后续使用 | IMPLEMENTED_BUT_UNVERIFIED | 路径存在，本轮未核验足以覆盖 C1–C5 的真实逐请求证据；不由上述预置测试推导 |
| 同 Run 新记录/替代即时刷新 | PARTIALLY_IMPLEMENTED | submit/steering 可重新召回，普通 tool turn 后不刷新 |
| quote 支撑 content、用户授权替代、隐私硬约束 | DESIGN_GAP | parser 校验原文存在，而非蕴含/授权；Prompt 规则不能当硬校验 |
| 来源变更后的失效 | NOT_IMPLEMENTED | 无文件 revision/hash/current-source revalidation |
| Record Delete/Invalidate、永久禁止旧条目复活 | NOT_IMPLEMENTED | Store 仅 active/superseded，重复 upsert 可 active |
| Record×Compaction 长上下文事实保留 | PARTIALLY_IMPLEMENTED | tests/test_context_compaction.py 证明结构/协议切点；eval/phase4 的 restore_after_compaction_preserves_context_and_plan 不验证 Record+最终请求 |
| Record Store 的 Replay 一致 | IMPLEMENTED_AND_TESTED | 上述 Record/Extraction restore tests，不等于 formal Continue Oracle |
| 正式 Continue 按当前 Record 状态拒绝旧快照 | DESIGN_GAP | Engine 327–343 + restore pending 277–303；尚无该组合测试 |
| Record/Memory 精确去重、预算优先 | PARTIALLY_IMPLEMENTED | context_builder.py 128–169；不识别语义冲突，不去重当前历史 |
| Record 自动跨 Session 沉淀 | NOT_IMPLEMENTED（Agent Loop 接线） | memory_auto_extraction.py 有 helpers；检索 build_auto_memory_input/commit_auto_memories 未发现生产调用方；不能把它们作为现有自动路径 |
| 完整 Record 最终请求 receipt | NOT_IMPLEMENTED | 现有专用 receipt 是 Memory；Eval 可透明抓最终请求 |

历史 Phase5 C3 与 L1 qualification 的局限：C3 预置事实，不验证真实 extraction；`eval/phase5c/l1.py:19–48` 的 grounding 只要求部分 token 重合，不是 quote-to-fact entailment Oracle。M3 必须另建独立 Ground Truth，不沿用弱判断宣称可信。

## 7. 最小建议：仅建议，不实施

P0：先复现三项：旧 Checkpoint 缓存注入、superseded 重复正文激活、合法 quote 搭配错误 content。再明确契约。

- 最小正确性方向：Provider 请求组装前及正式 Continue 依据 Session 当前 active ID/正文/来源复核缓存；不必照搬 M2 跨进程锁，因为 Session 的并发模型不同。若契约要求同 Run 新事实可用，还需明确完整 Turn 后重新选择记录的时点与 query，不是仅加“过滤”。
- 来源：硬性拒绝纯模型推断与证据内容不符；优先可验证的结构值/独立验收，不以另一个模型的自评替代。对于工具来源替代，先决定是否授权该能力；默认保留当前用户更正限制，不暗自放宽。
- 替代：明确旧正文重新出现是合法新事实还是陈旧回放；否则不能宣称永久 stale rejection。
- 受信手动 API 多 mutation 部分提交：先失败注入，再决定是否统一事件提交；不能把静态风险当已复现缺陷。

P1：Eval 透明 with_options/取消/错误传播代理，记录 MAIN_AGENT / CONTEXT_RECORD_EXTRACTION / COMPACTION 三种最终请求及真实 Usage；C4 使用现有预算 RunOptions，无新 Production flag；正式 continuation 若不可达应 BLOCKED。

P2：在正确性证据后考虑历史/摘要去重、去重与裁剪顺序、召回质量和预算。不得为了降低 Tokens 改掉 C1–C6 Ground Truth 或扩展为 M4。

实施方案、独立 Oracle 和准入门槛见 `eval/memory_m3/plan.md`。本轮结束，等待人工确认。
