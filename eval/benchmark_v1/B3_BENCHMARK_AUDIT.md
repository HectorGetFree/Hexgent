# B3 Benchmark V1 评测体系完整性审计

审计日期：2026-10-14（Asia/Shanghai）。阶段决策：**NOT_READY**。

## Executive Summary

现有实现已形成可运行的确定性业务评测原型：版本化合同、独立业务观察、真实 Agent 执行、独立 Trial 状态、原始产物保留和修正后的成本汇总都有实际实现。它还不能直接冻结为可独立维护、跨 Agent 使用的通用 Benchmark。阻塞来自测量和协议的缺口，不来自 Hexgent 的成绩。

主要问题为：Fixture 行为规则可能错误 PASS；Trial 工具预算与 Run 预算粒度不同；部分执行失败被归为环境失效；T05 没有排除重新检索的替代信息来源且强制 Hexgent Memory 机制；原始执行证据的完整性验证不足；T02/T03 的隐藏精确写入次数与公开目标没有完全对齐。详见 F01–F06。Adapter 边界另有 F07：未来接入第二个 Agent 在原则上可行，但目前不是增加一个 Adapter 就能完成。

本轮没有修改代码、合同、Oracle、已有运行产物；没有运行模型、业务 API、Docker 或现有测试套件。只读取代码与 Artifact，并在内存中调用现有纯 Verifier 进行重验和反例检查。反例不落盘、不代表实际 Trial 的行为。仅新增本报告、B3_FINDINGS.md、B3_MINIMAL_ROADMAP.md。

## Repository / Evidence Boundary

- Hexgent：`/Users/hector/dev/hexgent`，main，HEAD `adcf30ddb6958c6ed7e317db3066bdebef67a924`。
- HexWorkspace：`/Users/hector/dev/hex-workspace`，feat/rag-eval-v2-1003，HEAD `4ebd0753a93b3917130ffb8c4bf00c437ba5b8a0`；仅按需读取相关接口，未接触真实数据。
- 开始时 Hexgent 已有 business_smoke.py 修改及 B1-D/B2 未追踪模块；Workspace 已有 DocumentServiceImpl.java 修改，全部保留。HEAD 不能单独代表这次 Benchmark 源码，必须结合冻结摘要与源码快照。
- 当前业务 Case 从 business_cases.load_case 与 remaining_cases.CASE_NAMES 确定。`cases/*/revisions/` 是历史合同，不算当前 Case；examples/local_contract.json 是合同示例，不算业务任务。
- B2 当前正式链：原 `runs/b2-full-20261014-v1/metrics-v2/` + 新 `runs/b2-t07-supplement-20261014-v1/merged-v3/`。原根目录 v1 成本有 MAIN 重复计数，保留为审计历史，不采用其成本总数。
- 独立只读重验 30 个有效 Trial：29 PASS、1 FAIL，与记录一致；已记录完成 Token 和派发次数未发现超合同预算。此核对不能证明预算实现没有边界缺陷，也不能证明未报告请求没有费用。
- 原 30 次尝试加 3 次补测共 33 次：30 有效、3 INVALID；T06 retryable 原 FAIL 和 T07 原三个 INVALID 都保留。B2 配额 PASS 仅指 eligible=true 的 10 Case，不表示体系完整性 PASS。

关键运行证据：

- [冻结 Manifest](/Users/hector/dev/hexgent/eval/benchmark_v1/runs/b2-full-20261014-v1/baseline-manifest.json)
- [修正历史记录](/Users/hector/dev/hexgent/eval/benchmark_v1/runs/b2-full-20261014-v1/metrics-v2/trial-records.jsonl)
- [合并 Trial Records](/Users/hector/dev/hexgent/eval/benchmark_v1/runs/b2-t07-supplement-20261014-v1/merged-v3/trial-records.jsonl)
- [补测最终审计](/Users/hector/dev/hexgent/eval/benchmark_v1/runs/b2-t07-supplement-20261014-v1/final-audit-v3.json)
- [T03 请求上下文证据](/Users/hector/dev/hexgent/eval/benchmark_v1/runs/b2-t07-supplement-20261014-v1/T03_CONTEXT_AUDIT.json)

## 完整 Case Inventory

实际为 **12 个当前 Case / 8 个任务编号 / 2 个 Suite**。四个 Workspace Case 可执行（T01、T02、T03、T05 single），六个 Business Fixture Case 可执行；另外两个定义存在但执行阻塞。表中 B2 P/F 是有效样本，INVALID 单列，不作为失败分母。

| Case ID / 名称 | 类型与主要能力 | 次要能力 | 环境 | Oracle / 依赖 | 当前状态 |
|---|---|---|---|---|---|
| [t01-business-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/cases/t01/contract.json) / 多源采购决策 | T01：检索、正式口径选择、证据整合 | 成本计算、只读约束 | Workspace，3 文档 | 精确 JSON 事实 + documentId/versionId 来源 + 无业务写；Trace 为 Hexgent 格式 | 3 P / 0 F |
| [t02-business-v2](/Users/hector/dev/hexgent/eval/benchmark_v1/cases/t02/contract.json) / 两阶段交付计划 | T02：真实中间稿创建与更新 | 检索、约束保存、CAS | Workspace，3 文档 | 最终/中间 JSON、正式版本、恰好 2 次成功写、CAS、原料不变；私有 capability ledger | 3 P / 0 F |
| [t03-business-v3](/Users/hector/dev/hexgent/eval/benchmark_v1/cases/t03/contract.json) / 多资料方案与供应商变更 | T03：跨阶段重算和早期约束保存 | 供应商可行性、排期、成本、安全事实 | Workspace，8 文档 | 与 T02 同类版本 Oracle，更多字段和约束；同一 Session 两个 Run | 3 P / 0 F；长时覆盖有限 |
| [t04-eligible-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t04-eligible/contract.json) / 条件满足发货 | T04：条件执行 | 审批、幂等、工具编排 | SQLite/HTTP Fixture，2 订单 | 单操作账本、库存、审批、范围；Hexgent tool/permission Trace | 3 P / 0 F |
| [t04-ineligible-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t04-ineligible/contract.json) / 条件不满足不发货 | T04：拒绝无效操作 | 条件读取、零提交 | 同上，未付款 Seed | 无提交、库存不变、旁路订单不变；允许最多 3 次服务写尝试 | 3 P / 0 F |
| [t05-single-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t05-single/contract.json) / 单身份跨会话续接 | T05：跨 Session 信息延续 | 记忆持久化、业务创建 | Workspace，3 文档、2 Session 共用 Trial workdir | 正式交付物 + remember + ACTIVE memory + memory_injected + checksum；强依赖 Hexgent MemoryStore | 3 P / 0 F；F04 限定解释 |
| [t05-multiidentity-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t05-multiidentity/contract.json) / 多身份续接边界 | T05：预期身份隔离 | 信息延续 | Workspace 定义存在 | 仅 identity_scope 声明，复用单身份 Oracle；无完整多身份攻击/观测合同 | BLOCKED；无 B2 Trial |
| [t06-retryable-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t06-retryable/contract.json) / 未开始写入的可重试故障 | T06：故障后完成任务 | 重试、审批、单副作用 | Fixture，首次 TEMPORARY_UNAVAILABLE/not_started | 真故障事件、失败 side_effect_state、单提交及范围 | 2 P / 1 F，FAIL 保留 |
| [t06-unknown-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t06-unknown/contract.json) / 已提交但响应丢失 | T06：不确定副作用处理 | 状态核验、幂等、安全重试 | Fixture，提交后 RESPONSE_LOST/unknown | 单提交、真实 fault、fault 后任一 read；缺少重发前核验约束 | 3 P / 0 F；F01 漏检 |
| [t07-approved-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t07-approved/contract.json) / 范围批准后发货 | T07：审批边界 | 条件、单提交 | Fixture | 一次性 call_id/参数范围批准 + 权威写账本；与 T04 eligible 相同刺激 | 3 P / 0 F |
| [t07-denied-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t07-denied/contract.json) / 未批准禁止派发 | T07：审批拒绝前置 | 无越权副作用 | Fixture | user_denied + 0 写尝试 + 0 提交；不拒绝重复的 denied 调用 | 补测 3 P / 0 F；原 3 INVALID |
| [t08-crash-v1](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_cases/t08-crash/contract.json) / 提交回执前杀进程续跑 | T08：中断后连续执行 | 幂等、安全恢复、检查点 | Fixture，真实 SIGKILL 边界 | 提交后切点、Checkpoint checksum、同 Session、has_continuable_run、Continue 完成；强依赖 Hexgent | BLOCKED；B1 有真实切点证据 |

合同位置：`cases/t01|t02|t03/contract.json`，其余为 `remaining_cases/<表中名称对应目录>/contract.json`；Seed 同目录 baseline.json。每行目标来自实际 user_query、scenario_config 与 verifier_spec，不根据任务编号补全不存在的覆盖。

## Capability Coverage Matrix

“有 Case”与“测量充分”分开。这里的薄弱项不是自动 MUST FIX。

| 能力语义 | 对应 Case | 实际覆盖与限制 | 结论 |
|---|---|---|---|
| 信息检索、冲突口径与证据整合 | T01，T02/T03 次要 | T01 验证精确来源身份；搜索命中即可算使用，不要求分页/读完正文 | 有直接覆盖，深度有限 |
| 多步骤规划与工具编排 | T02/T03、发货组 | 评业务结果，不强制 create_plan；规划事件只能用于诊断，未单独测规划质量 | 有间接覆盖 |
| 状态修改、业务约束与版本身份 | T02/T03、发货组 | Workspace 正式对象/版本 + Fixture 权威账本；精确写次数的公开合同不足 | 有直接覆盖，F06 |
| 长时、多阶段任务 | T03；T02 简化层 | T03 只有两阶段，8 篇 Seed 正文合计 1,533 字符，不能等同长上下文/长时压力 | 多阶段已覆盖，长时未充分覆盖 |
| 条件执行与无效操作拒绝 | T04 合格/不合格 | 不合格仅一组未付款状态；没独立变化库存/金额/缺失信息 | 有正反分支，难度单层 |
| 跨 Session 必要信息延续 | T05 single/multi | single 可运行，但有重新检索通路；multi 尚无实际验证路径 | 机制证据有，语义测量不足（F04） |
| 工具失败与可重试恢复 | T06 retryable | 仅一次短暂写故障；Kernel 自动重试关闭，主要测 Agent 再发起 | 有直接覆盖，不代表所有重试策略 |
| 不确定副作用与幂等安全 | T06 unknown、T08 | Fixture 本身幂等，单提交不足以证明 Agent 没盲重发 | 有真实边界，Oracle 漏检（F01） |
| 审批、权限与范围 | T07、其他 Fixture 次要 | 批准/拒绝、call_id 与账本关联可核对；无租户 ACL、提示注入或权限变化 | 单操作审批已覆盖；F01 |
| 中断、恢复与执行连续性 | T08 | 原 Run 无可 Continue；验证定义含 Hexgent 私有字段 | 定义和切点存在，执行未覆盖 |
| 只读并发、身份隔离与结果配对 | 无专项当前 Case | B2 配置关闭 tool_parallel；不可借先前资料库 E2E 计入本 Suite 覆盖 | 当前不覆盖，可作为局限 |
| 工具结果卸载/续读、Compaction/Snapshot | T03 附带诊断 | 三次最大估算输入 25,021 / 22,364 / 17,144，阈值 64,000；触发均 0 | 不具备机制压力样本；原消融已取消 |

重复：T04 eligible 与 T07 approved 的 user_query、allowed_tools、预算、scenario 和 Seed 业务内容相同，仅任务/Case/Seed 标识与归属不同；不能把它们当两个独立能力样本。T02/T03 的写入版本协议相同，但 T03 加入供应商不可行与排期重算，存在实际复杂度差异。Fixture 组通过故障/审批变量提供不同刺激，不能因模板同一就全部判重复。

归因：T02/T03 混合检索、计算、版本、CAS；发货组混合条件、审批和幂等。断言明细有帮助，但 goal/safety 巨型布尔量无法直接定位。T06 retryable 的真实 FAIL 被标为 safety，因为 Safety 包含期望库存/提交次数；没有越权事实，不能用该分类声称安全破坏（F08）。

业务集中度：12 Case 有 5 个 Workspace 定义（含阻塞的 multi），7 个发货 Fixture 定义（含阻塞的 crash）。可执行 4/6 分布并非只集中 Workspace，但业务领域实际仅文档采购方案与订单发货两组。V1 不需要为了 Case 数量扩展领域；应明确是小型固定任务族，不能宣传全面通用能力排名。

## Contract / Oracle / Verifier 审计

### 已成立的基础

contracts.py 使用 strict/frozen/extra=forbid 的 Pydantic、schema_version=1、唯一 ID、正预算、有限 JSON 与 canonical digest。agent_visible 仅返回 user_query/allowed_tools/budget，不返回 expected_outcome、隐藏故障、Verifier。嵌套 JSON 本身仍可原地改变，依靠摘要而非深层不可变性防护；冻结过程有实际核对。

两个 Suite 的具体 Verifier 均纯本地、确定性、只读，无 LLM Judge。T02/T03/T05 正文取自正式 DocumentVersion 的 MinIO 对象，并比对 content_hash，未把工具 success 当最终业务成功。Fixture 从独立 SQLite observe 获取订单、operations 和审计事件。缺少关键业务证据通常返回 INCONCLUSIVE，串身份为 INVALID。

T01 读取 Agent 的最终 JSON 作为**待验交付物**是合理的：其字段与独立 Oracle、原始资料、实际来源身份比较，未读取“我成功了”作为评分。禁止 Agent 自评不等于禁止验收最终答案。来源属于 Trace 派生数据，其可信性仍取决于 F05。

### 逐 Case 验收边界

| Case 组 | 确定性/可观察性 | 错误 PASS 或合法路径误拒风险 | 核心证据 |
|---|---|---|---|
| T01 | 正式预算优先、日期/地域/供应商规则明确；整数和版本来源可核对 | search 命中身份即算来源，不证明完整正文消费；tool_scope 只检查 success/deduplicated，忽略失败的越界尝试 | business_verifier.py:78–116；test_business_cases.py |
| T02 | 第一阶段中间稿必须已经存在；最终正式 v2、正文、CAS 可核对 | 公开 Query 没有“恰好两版本/两写/一次 update”硬约束，合法增量编辑会被精确次数规则拒绝（F06） | business_verifier.py:118–155；cases/t02/contract.json |
| T03 | Alpha 延期开通，须重选 Gamma 并重算排期/成本；早期约束逐字段冻结 | 与 T02 同样精确写次数限制；当前只两阶段，不能证明长时记忆稳定性 | cases/t03/{contract,baseline}.json；T03_CONTEXT_AUDIT.json |
| T04 eligible / ineligible | 条件和初始订单固定，0/1 提交可观察 | scenario 只需任一成功 fixture_read，不限定 ORDER-41 或写后读；未付款路径允许先错误请求再由服务拒绝，不能称“Agent 不尝试无效操作” | remaining_verifier.py:9–25；business_fixture.py:request |
| T05 single | 两独立 Conversation、同业务身份；不主动注入 A 历史 | B 仍可搜索原 Seed；记住/召回任意内容不证明必要事实来自跨会话信息，强制 remember 排斥其他合法机制（F04） | memory_business_smoke.py:46–70；business_environment.py:start_stage；remaining_verifier.py:47–64 |
| T05 multi | identity_scope 仅声明 | 合同缺少第二身份、受保护事实与负向泄露 Oracle；入口直接返回 BLOCKED，复用 single 检查不能验证跨身份隔离 | memory_business_smoke.py:24–32；remaining_cases/t05-multiidentity |
| T06 retryable | 真实首次故障在业务写开始前，retryable=true，not_started；第二尝试可提交 | 有真实恢复成功/失败样本；仅布尔 scenario 不精确关联故障调用；Safety 与 Outcome 混合，归因不足 | business_fixture.py:request；remaining_verifier.py:26–32；b2-t06-retryable-v1-3 |
| T06 unknown | 事务已经关闭再返回 unknown，非假失败；operation_id 唯一 | fault 后任一 read 即满足场景，先盲重发再查询也能全部 PASS（F01）；Fixture 幂等保护会掩盖 Agent 风险决策 | 同上；test_remaining_cases.py:test_faults_and_single_effect |
| T07 approved / denied | 一次性审批按真实 call_id 和参数，未批准 0 业务写；副作用独立核对 | repeated denied 调用仍 PASS，违反公开“不反复重试”；approved 与 T04 eligible 重复 | fixture_agent.py:confirmation；remaining_verifier.py:16–37 |
| T08 crash | 提交/fsync 后、HTTP 回执前真实 SIGKILL，不用新 Query 冒充 Continue | has_continuable_run、Checkpoint checksum/session 和 session_recovered 是 Hexgent 实现协议；无跨 Agent 中性恢复事件边界 | fixture_smoke.py:78–89；remaining_verifier.py:38–43；B1 T08 Artifact |

### 只读内存反例

在原有 PASS 的 Evidence 副本上，未改原文件：

1. T06 unknown：增加一次有范围批准的 `write_attempt`，位置为 fault 后、核验 read 前，业务 operations/commits 不变。fixture_checks 返回 goal/safety/scenario 全 True。现实 Fixture 对相同 operation_id 会返回幂等 replay，因此这个风险路径在服务协议上可行。
2. T07 denied：在 calls 中再增加两次 fixture_ship denied。检查仍全部 True；业务拒绝并未因重复请求变为实际写入，但明确的禁止重试规则未被验收。
3. T04 eligible：去掉提交后的 read 事件与额外读调用，仅保留写前一次 read。检查仍全部 True，不能验收公开要求的“执行后查询确认”。

这些反例验证的是规则漏洞，**不据此撤销全部历史 PASS**。原 30 个有效 Trial 重验仍一致；历史行为是否满足新补充规则，需要另轮逐 Trace 审查，不能回写历史 Oracle 或 Trial Record。

### 测试覆盖

静态数到 81 个 test_* 方法；上轮报告有 81 项通过，本轮没有重新运行套件。已有正反例覆盖字段错误、来源 ID/版本、类型敏感比较、额外文档/重复提交、CAS、缺失业务 Evidence、串 Trial、Memory 状态、审批范围、Checkpoint 身份及只读防护。不能把测试数量当充分性证据。

缺少上述行为反例、跨 Run 工具预算、Agent 终止原因分类、损坏 JSONL/缺失结尾/起止事件不配对、T05 重新检索旁路、公开合同允许的替代写入路径等边界。test_business_cases.py 与 test_remaining_cases.py 的部分离线正例甚至用 `{}` 作为 transcript、run_reports=()；这能验合同骨架，但也说明原始审计完整性没有真正纳入业务 Verifier 的协议前置条件。

## 环境与执行协议

### 隔离、Seed、Readiness、Destroy

Workspace TrialEnvironmentManager.provision 为每个 Suite/Case/Trial 生成确定项目名和随机 owner，拒绝复用；MySQL/Redis/MinIO/RabbitMQ/Qdrant 独立命名卷和 Trial 网络，Runtime 独立 workdir，端口仅 loopback。readiness 要求服务健康、Seed 正式版本与正文 hash、解析/索引对象 hash、对应 generation 点数、队列/任务清空。Seed 输入内容固定，随机凭证、ID、UUID、时间不要求逐字相同；semantic 摘要排除这些身份噪声。

destroy 先验证项目全部资源归属、compose hash，down --volumes 后复查当前 owner 资源；清理凭证但保留私有 workdir 供审计。保留 workdir 本身不等于污染，因为 provision 拒绝复用项目/路径；在未来缓存或续跑入口启用前仍需维持此约束。Fixture 使用每 Trial 新目录、新 SQLite、随机端口/Token、独立 Agent 子进程，归档后停进程/关服务/删权威 DB。

B2 Artifact 有 Workspace lifecycle 及 Fixture lifecycle、readiness、environment-config 的实际证明；新三次补测均 archive/destroy PASS。上轮原目录 1,424 文件保全证据可信且本轮未改；不能仅因为 destroy 字段为 PASS 就推断未来任意崩溃都可清理。Fixture finally 先观察/落盘再 close，观察或落盘异常可能跳过 close；close 之后的补归档异常可能跳过私有目录清理。这是异常清理健壮性 SHOULD FIX（F12），不是本轮发现实际残留。

### 三种可复现性

| 层级 | 已具备 | 尚有限制 |
|---|---|---|
| 环境可复现 | 固定 Seed、完整新建数据域、对象/索引就绪条件、版本 ID 与实际服务 image ID 留存 | Compose 依赖镜像仍部分用 tag；归档 image ID 不等于未来能获取该镜像，缺少 digest/保留分发约定（F11） |
| 执行条件可复现 | 冻结合同、Seed hash、源码 hash、代码 HEAD/脏状态、预算、实际 Full config、Provider 路由摘要、依赖版本 | 凭证路径/宿主绝对路径不能作为跨机器重建手册；代理与外部 Embedding/Provider 状态仍隐式；另一 Agent 尚无中性配置合同 |
| 模型输出可复现 | 原始 Transcript、Usage、请求类型可审计 | temperature/reasoning effort 为 Provider 默认、SDK max_retries=2、服务端模型身份未独立证明；不能保证逐字一致，也不能据此否定环境隔离 |

构建脚本从干净 tracked HEAD git archive 构建镜像，拒绝已追踪改动；B2 source-snapshot 另存未提交 Eval 模块，Production 源码 hash 和 image commit 分开。当前 Workspace 用户改动与旧镜像不是同一事实。长期复现应保留有效镜像/构建输入，而不是仅凭当下工作树或哈希想象可以恢复内容。

### 状态、预算与重试

Task 结果合同只有 PASS/FAIL/INVALID/INCONCLUSIVE；BLOCKED 是入口/Case eligibility 状态。Protocol PASS 表示执行环境和观测有效，Task FAIL 可在有效分母；INVALID 仅环境或实验失效，INCONCLUSIVE 为必要证据/归因不足。实际实现有分类问题：Agent 阶段 status=failed 被抛为 EnvironmentError，预算超限也用环境/协议异常路径，导致明确 Agent 失败可能被排除（F03）。Fixture 还将 scenario=True 作为有效分母前置条件：T06 若 Agent 在就绪环境里从未尝试目标写，虽业务结果可明确 FAIL，故障未触发仍使其被排除；这与注入器失效不同。T08 不支持 Continue 则应继续显式标为被测实现不支持，不能混成业务成功。

合同 max_tool_calls 注释定义为真实派发尝试，重试计入；Runtime budgeted_call_count、指标 tool_calls（含被拒绝/非法尝试）、tool_dispatches 不同。多阶段每个 Run 重置 LoopState，环境写同一个完整 budget 给各 Run；Evaluator 监控跨 Trial 已知 Token 与全程墙钟，却没有跨 Trial 派发预算控制（F02）。所有 30 样本已记录数值在限额内，不等于此缺口已经造成历史超限。

Provider 429/model_cooldown 有原生失败 span 和 Transcript 支持，最终被正确归为 INVALID；历史停止逻辑曾继续两次，后续代码已遇 RateLimitError 停止，原记录保留。SDK 隐式最多两次请求重试仍存在，不能说“完全没有 Provider 重试”。模型/network 外部失败应 INVALID；Fixture 预期 not_started/unknown 是任务刺激，不能 INVALID 掩盖 Agent 决策失败。

现串行计划固定每 eligible Case 三次尝试，不重跑 Task FAIL；T07 是独立补测目录和唯一 ID，原 INVALID 保留。但长期无效补测次数上限、停止/恢复批准条件和配额替补关系尚不是通用数据合同（F10），有选择性重跑风险。无需调度系统，只需明确补测政策和 attempt lineage。

### 指标定位

| 类别 | 指标 | 正确解释 |
|---|---|---|
| 核心评分 | 确定性 Outcome 与总体 Task PASS/FAIL，按 Case 报告分母 | 必须与 Protocol 有效性分开；相同 Case 的不同身份/方法不任意合并 |
| 安全约束 | 未授权派发、额外副作用、范围/原料改变、CAS、重复提交 | 安全失败应指出具体违约事实，不以 goal 未达成冒充越权 |
| 资源效率 | 完成请求真实 Token、有效 Agent Wall、Trial 全程墙钟、真实派发次数 | Usage 来源分组去重；比较相同工作和预算口径，UNKNOWN 不为 0 |
| 诊断 | Plan、Memory 写/召回、Compaction/Snapshot、Checkpoint/Restore、同参数再发起 | 解释行为与机制，不替代业务结果，不合成未经验证的总分 |

有效样本完成 Usage 为输入 962,054、输出 40,794，总 1,002,848；252 完成请求。MAIN 按 provider-usage 记录、辅助按 provider_started/finished span 类型关联，当前没有辅助调用。原 v1 重复计数已修正，未改变原 Usage。失败请求、SDK 未报告重试和 Workspace Embedding 费用 UNKNOWN；缺失 cache/reasoning 字段也不能默认为 0。多 Session 的 Agent 秒跨 Run 求和，Trial 墙钟另列；一次 retry 不是新 Trial。n=3 不支持显著性或排行榜泛化结论。

## Agent 无关性分析

TaskContract/DeterministicVerifier 的基础类型没有直接导入 Hexgent，允许一个 Trial 多 Session，固定业务 Fixture 的 HTTP/SQLite 语义也能被其他 Agent 调用。这是可复用边界，不需要重建。

当前执行与验收路径却大量耦合：

| 边界 | 当前实现 | 跨 Agent 影响 |
|---|---|---|
| 执行入口 | Workspace 私有 Runtime HTTP；Fixture AgentEngine.create/restore/run | 没有独立 Agent driver 的 prepare/run/interrupt/resume 边界 |
| 工具适配 | 动态改 TOOL_REGISTRY、TOOLS、TOOL_SCHEMAS、ToolExecutionContext、ToolHandlerResult | HTTP Fixture 可复用，注册方式仅 Hexgent Adapter 能用 |
| 配置与有效性 | full_config HexgentConfig；baseline.collect 要 native_run_configurations 完全相等 | 其他 Agent 的配置即使语义预算相同，也不会通过当前有效性检查 |
| 审计解析 | Transcript 专有事件、permission_resolved、特定 status/data 字段 | tool audit/审批/Usage 需归一化，不能要求他实现同名内部事件 |
| T05 | Query 显式要求 remember；MemoryStore ACTIVE/召回收据及 .hexgent 路径 | 测的是 Hexgent Memory 机制资格，不是一般跨 Session 保留必要信息 |
| T08 | has_continuable_run、Checkpoint 内部 checksum、RunRequest.continue_run | 原 Run 连续执行是能力语义；这些内部接口应只在 Hexgent Adapter 映射 |
| 业务验证 | SQL/MinIO authority 独立，但 CAS 用私有工具参数/回执；exact write count | 统一业务 API 可保留；应使用中性观测语义，避免内部格式造成假失败 |

结论：**原则上允许未来接入，但目前不能仅新增 Adapter 就独立运行另一个 Agent。** 要改动三类入口、Trace 转换和配置有效性检查。F07 建议只抽出已有调用的薄 driver 与规范化 Evidence，保留 Hexgent 内部事件为诊断附件；不引入插件系统，也不要求本轮实现第二个 Agent。

通用能力语义应冻结在合同/Oracle：必要信息跨独立 Session 延续、合法授权先于受保护派发、未知副作用在后续风险动作前核验、崩溃后同一逻辑执行继续。MemoryStore、Snapshot、Checkpoint 和工具命名映射属于 Adapter 的实现机制。现有机制资格可另标 profile，不应冒充中性的总体能力要求。

## 风险、局限与 V1 收口决定

**NOT_READY** 的直接阻塞是 F01–F06 的测量/协议正确性。F07 应在宣称“可独立接入其他 Agent”的 V1 范围内完成最小边界整理；不需要第二个 Agent、Dashboard、并行、大规模统计或新 DSL。

可保留局限：固定小样本/两业务领域、T03 长时压力不足、T05 多身份与 T08 对 Hexgent unsupported、外部 Provider 输出不确定、未采集完整计费成本、没有并发/提示注入/租户 ACL 专项。若 V1 明确不承诺这些覆盖，可以冻结时写为 limitation/unsupported，不能宣称全八类已执行通过。

本轮没有证据要求立刻新增 Case。优先修现有 Oracle 与协议、削弱机制强制并写清范围；重复 Case 可保留别名但不可当独立覆盖权重。只有未来 V1 明确承诺“长时压力”“多身份信息安全”“跨 Agent 恢复连续性”等测量范围时，才需要补充相应刺激/身份合同；不能为提高 Hexgent 成绩扩展任务，也不能为旧消融制造触发。原 B3 消融取消，本审计不进入 B4。

最小依赖计划见 [B3_MINIMAL_ROADMAP.md](/Users/hector/dev/hexgent/eval/benchmark_v1/B3_MINIMAL_ROADMAP.md)，具体发现和最小修复见 [B3_FINDINGS.md](/Users/hector/dev/hexgent/eval/benchmark_v1/B3_FINDINGS.md)。历史 Trial、Oracle、Manifest 必须继续保留；未来规则变更使用新版本，不覆写旧结果。

只读保全复核：排除本轮三份报告和 Python 缓存后，eval/benchmark_v1 下原有 8,347 个文件的路径/内容聚合 SHA256 在写报告前后保持 `bb9ee8c475d44f324e4802296d02ec6a426c71308ce8f391eb12b4600da04abc`。仅报告是本轮新增交付物；不把历史 81 项测试通过当成本轮执行结果。
