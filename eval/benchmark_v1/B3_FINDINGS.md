# B3 Findings

审计日期 2026-10-14。决策 **NOT_READY**。代码根 `/Users/hector/dev/hexgent/eval/benchmark_v1/`。所有发现依据本轮实际源码；内存反例不代表历史 Trial 出现该行为，不自动撤销历史成绩。MUST FIX 关注可信度，SHOULD FIX 关注覆盖/移植/维护，OPTIONAL 不阻塞 V1。

## MUST FIX

### F01 — Fixture 行为规则可以错误 PASS

- 受影响：T04/T06/T07；[fixture_checks](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_verifier.py:9)、[业务请求](/Users/hector/dev/hexgent/eval/benchmark_v1/business_fixture.py:69)。
- 证据：scenario 只要求任一成功 fixture_read；T06 unknown 只要求 fault 后有 read；T07 denied 禁止实际写但不统计重复的 denied 调用。只读内存反例分别加入未知提交后的盲重发、两次 denied 重试、删除提交后的读，检查都返回 goal/safety/scenario=True。测试目前覆盖单提交、缺 fault、审批范围，但没有上述违约路径。
- 影响：幂等服务掩盖风险决策；缺少查询确认或违反禁止重试仍可 PASS。不能据当前 PASS 推断这些能力已被完整测量。
- 最小修复：按权威事件的必要前置关系验收（未知结果→匹配目标/操作的核验→后续风险写），不要求固定整体工具顺序；拒绝后不得重复目标写调用；明确要求目标订单的写后核验。审批证据按 call_id/工具/参数对齐，不读取 Agent 自述。为当前遗漏添加负例。
- 阻塞 V1：**是**。修复采用新 Verifier/协议版本，旧运行原样留存，不为 Hexgent 提高分数放宽断言。

### F02 — Trial 工具预算没有跨 Run / Session 执行

- 受影响：T02/T03/T05；[预算合同](/Users/hector/dev/hexgent/eval/benchmark_v1/contracts.py:40)、[环境配置](/Users/hector/dev/hexgent/eval/benchmark_v1/business_environment.py:47)、[Trial 等待](/Users/hector/dev/hexgent/eval/benchmark_v1/business_smoke.py:78)、[Run 计数](/Users/hector/dev/hexgent/hexgent/loop/loop_state.py:162)。
- 证据：合同 max_tool_calls 是 Trial 派发预算；环境给每个 Run 同一完整 max_tool_calls，LoopState 每 Run 重新累计。wait_stage 只查 Trial 已知 Token 和墙钟；baseline.collect 不检查累计派发超限。报告 tool_calls 含拒绝/非法调用，和合同派发尝试不同。
- 影响：多 Run Agent 理论上可消耗两份工具预算而仍满足 Protocol/配置判断；不同 Agent 的 Run 划分改变资源条件。现 30 样本未发现已记录派发超额，不说明历史结果已失效。
- 最小修复：在 Eval/Adapter 边界累计 Trial 的真实派发尝试和剩余预算，阶段切换不重置；尝试/派发/处理器/重试分别记录。完成请求 Token 与 UNKNOWN 请求分开，不承诺无法观测的严格计费总额。
- 阻塞 V1：**是**，影响核心可比性；不改 Production Loop。

### F03 — 明确 Agent 失败可能被排除为协议/环境失效

- 受影响：Workspace 和 Memory Runner；[阶段错误处理](/Users/hector/dev/hexgent/eval/benchmark_v1/business_smoke.py:170)、[失败归档](/Users/hector/dev/hexgent/eval/benchmark_v1/business_smoke.py:242)、[Memory 阶段处理](/Users/hector/dev/hexgent/eval/benchmark_v1/memory_business_smoke.py:51)、[Fixture 超时处理](/Users/hector/dev/hexgent/eval/benchmark_v1/fixture_smoke.py:109)。
- 证据：Runtime status=failed 被抛为 EnvironmentError→BLOCKED，缺 Verifier 结果时创建 INVALID/environment。Token/墙钟超限也进入异常→环境/协议失效路径，未按终止原因区分有效 Agent 未完成和服务不可用。fixture_smoke.py:108 又把 scenario=True 作为 Protocol PASS 前置条件：T06 环境已就绪但 Agent 从未尝试写，业务 Verifier 可以明确 FAIL，故障未被触发却使其退出有效分母。
- 影响：能力不足、预算耗尽可能从成功率分母消失，得到选择性有效样本。不是声称本轮 T06 已被排除；T06 retryable 原 FAIL 在有效分母中保留。
- 最小修复：冻结终止原因映射：环境已就绪且 Agent 未达目标/耗尽执行预算为有效 FAIL；外部 Provider 429/网络不可用为 INVALID；证据不足 INCONCLUSIVE；被测实现不支持的能力另记 unsupported。区分故障注入器失效和 Agent 没有走到故障动作；后者不能掩盖已经成立的任务失败。保留 partial 业务状态/Trace，避免混用 EnvironmentError。
- 阻塞 V1：**是**。需要终止原因离线反例，不需要重跑模型。

### F04 — T05 不能隔离跨 Session 信息来源，且强制特定 Memory 机制

- 受影响：T05 single/multi；[T05 Runner](/Users/hector/dev/hexgent/eval/benchmark_v1/memory_business_smoke.py:46)、[阶段工具范围](/Users/hector/dev/hexgent/eval/benchmark_v1/business_environment.py:81)、[Memory 检查](/Users/hector/dev/hexgent/eval/benchmark_v1/remaining_verifier.py:47)、remaining_cases/t05-single/contract.json。
- 证据：Session B start_stage 仍公开 search_workspace/get_document/list_folder；原始 Seed 仍在同 Workspace。scenario 仅要求某个 remember 的 ID 被真实 recall，不检查该信息是否为交付所必需；正确交付可来自重新检索，同时召回无关记忆。实际 B2 single 的 B 阶段工具为 create_document，支持本次使用了续接，但不能证明协议排除了旁路。公开 Query 与验收又强制 remember/MemoryStore ACTIVE/memory_injected，合法替代记忆机制会被排除。
- 影响：错误 PASS 风险与跨 Agent 误拒并存；测机制资格而不是“必要信息在独立会话仍可用”。
- 最小修复：在版本化实验协议中限制 B 的原资料重新取得通路，明确允许的持久信息接口与新会话边界；将必要信息延续作为语义 Outcome，将 Hexgent 写入/召回收据作为 Adapter 证据或专属 profile。不能把禁止直接历史注入误当所有旁路都已隔离。multi 在身份刺激/负向泄露 Oracle 完整前继续 unsupported。
- 阻塞 V1：**是**，若 T05 继续作为通用能力评分；若暂时降为明确的 Hexgent Memory 资格专项，必须同步收窄 V1 覆盖声明，不能称该语义已完整覆盖。

### F05 — 关键原始执行证据没有结构/完整性前置验证

- 受影响：全 Suite；[records](/Users/hector/dev/hexgent/eval/benchmark_v1/business_smoke.py:24)、[证据可用性](/Users/hector/dev/hexgent/eval/benchmark_v1/verifier.py:43)、[Session 检查](/Users/hector/dev/hexgent/eval/benchmark_v1/verifier.py:105)、[Session 合同](/Users/hector/dev/hexgent/eval/benchmark_v1/contracts.py:133)。
- 证据：records 对每条非法 JSON 都 continue，不只忽略正在写的最后半行；业务 verifier 验收派生 calls，却不与完整原始 Trace 对账。session_artifacts 主要检查文件存在，可不指定 digest；run_reports 可空且不解析。离线业务正例用 `{}` transcript 和空 reports 仍 PASS。Missing 文件有反例，但损坏/截断/缺结束事件的情况未封口。
- 影响：原始证据损坏可能丢掉越界调用仍保留 PASS；报告称“审计文件可读取”强于实际存在性检查。当前原始 30 有效样本重验一致，不能由此证明每种损坏都被检测。
- 最小修复：活动读取仅容忍未闭合尾行；归档后严格解析全量 JSONL、序号/身份和必须完成的事件配对；规范化审计与原始事件对账。归档 manifest 给必要文件 digest，读结果时校验。缺证据 INCONCLUSIVE，身份/协议损坏 INVALID，不能猜 PASS。
- 阻塞 V1：**是**，保证可长期离线审计；不需签名服务或通用 Artifact 平台。

### F06 — T02/T03 公开任务与隐藏精确写入约束未对齐

- 受影响：[版本验收](/Users/hector/dev/hexgent/eval/benchmark_v1/business_verifier.py:125)、cases/t02/contract.json、cases/t03/contract.json。
- 证据：version_chain 恰好两版本；side_effects 恰好两成功 mutations；cas 恰好一次成功 update。公开 Query 要真实中间稿与修订已有文件，不要求全程恰好两次写。合法的分段编辑/额外中间版本会被拒绝，即使最终事实、早期约束、原料安全和版本并发语义都正确。
- 影响：比较执行风格而非明确的业务目标，跨 Agent 易出现假 FAIL。精确 JSON 字段/文件名是公开要求，不属于这个问题。
- 最小修复：先冻结究竟测“严格两次写协议”还是“合法业务修订”。若精确次数是必要约束，版本化公开任务应明确告知；若评业务语义，需独立论证合法版本链验收及副作用边界，而非为了提高 Hexgent 分数放宽。旧 Oracle、Case、结果不覆写。
- 阻塞 V1：**是**，属于 Contract/Verifier 一致性，不要求现在新增 Case。

## SHOULD FIX

### F07 — Agent Adapter 边界尚不独立

- 文件：fixture_agent.py:register_fixture、business_environment.py:start_stage、business_smoke.py:audit/wait_stage、baseline.py:collect、remaining_verifier.py:memory_checks/fixture_checks。
- 证据：依赖 TOOL_REGISTRY/ToolHandlerResult/AgentEngine、Hexgent Transcript 字段与完全相等的 HexgentConfig；T08 要私有 Continue/Checkpoint 字段；没有 Agent ID/Adapter 版本和中性执行入口。
- 影响：原则上可适配别的 Agent，但需改三种入口和有效性判断，不能承诺“新增一个 Adapter 即可”。
- 最小建议：用薄 prepare/run/interrupt/resume + 规范化 tool/approval/usage/session Evidence 包装现有入口；业务 Oracle 不读私有 Memory/Checkpoint 字段，内部事件作为附件/profile。不引入插件或调度架构，不要求第二个 Agent。
- 阻塞 V1：对限定 Hexgent Adapter 的初版可作为局限；对“可独立接入不同 Agent”的发布承诺 **是**。本审计的 NOT_READY 首先由 F01–F06 决定。

### F08 — Outcome 与安全归因混合

- 文件：remaining_verifier.py:19–24，verifier.py:安全优先结果归类；Case：T06 retryable。
- 证据：Safety 将 expected shipment_count 参与库存/提交数核对；原 b2-t06-retryable-v1-3 未完成任务且提交 0，goal/safety 均 FAIL，failure_category=safety，权威账本无越权/重复提交。
- 影响：Task FAIL 有效，但类别不能区分未完成与安全违规。
- 最小建议：拆出未授权、范围、额外/重复副作用的事实断言与达成目标的 Outcome，维持总体严格性；不改写旧分类。表格报告两个轴。
- 阻塞 V1：否，若清楚解释旧分类；承诺安全失败率时需先修。

### F09 — 覆盖有重复、长时与难度层次不足

- 文件：cases/t03/baseline.json；remaining_cases/t04-eligible、t07-approved；T03_CONTEXT_AUDIT.json。
- 证据：两发货 Case 刺激一致；T03 正文 1,533 字符、两阶段，最大估算上下文不足阈值 40%；条件负例仅未付款；多身份与恢复无有效样本。
- 影响：Case 数不能当独立覆盖广度，现有 T03 支持多阶段重算，不支持长时压力结论。
- 最小建议：写清能力覆盖/unsupported；重复别名不独立加权；只有明确承诺相应能力时，才增加必要业务复杂度或补身份/恢复合同。本轮无新增任务。
- 阻塞 V1：否，只要收窄声明；若承诺全面长时/身份/恢复覆盖则阻塞。

### F10 — 有效补测政策与结果版本缺少统一合同

- 文件：baseline.py:SMOKES/inventory/report；baseline_closeout.py:main；baseline_metrics_v2.py:regenerate；runs/b2*。
- 证据：固定历史 Smoke 目录硬编码、planned_trials=30 写死；T07 单独补测 lineage 以 campaign 路径和 manifest hash 关联。regenerate 会写旧 Trial 的协议结果，当前审计未调用；新版 v3 以独立目录保留旧历史。通用 INVALID 替补上限/暂停恢复政策未定义。
- 影响：手工选 Smoke、替补直到成功、引用旧成本版本都有维护风险；现有 T07 证据透明，没有据此判定其选择性重跑。
- 最小建议：显式 active inventory/结果版本/尝试 lineage 与无效补测上限；FAIL 不替补；纠正统计只写新版本；从计划计算数量。无需完整 Runner 平台。
- 阻塞 V1：否，对当前冻结小集；长期运行规则发布前应完成。

### F11 — 镜像与外部条件可记录但未完全可取得/冻结

- 文件：environment.py:_configuration/archive；build_environment_images.py；baseline.py:freeze。
- 证据：mysql:8.0、redis:7.2、rabbitmq:3.13-management 等 tag，归档 actual_service_images ID，但新 Compose 未直接绑定这些 ID/digest；Provider 参数 omitted/default，SDK max_retries=2；Embedding 使用外部服务，费用和服务端版本未知。
- 影响：同机原镜像存在时重建有证据，不能保证另一机器以后从标签得到同条件。模型输出不确定与环境不可复现不是同一判断。
- 最小建议：固定镜像 digest/获取方式，记录非秘密出站/Embedding 型号版本与显式模型参数；注明无法控制的 Provider 条件，UNKNOWN 成本不补零。不要求复制密钥或保证模型逐字确定。
- 阻塞 V1：否，对限定可取得镜像的 V1；长期跨机器完全复现承诺需补。

### F12 — Fixture 异常观察/归档路径可能跳过清理

- 文件：[fixture_smoke.py finally](/Users/hector/dev/hexgent/eval/benchmark_v1/fixture_smoke.py:113)、business_fixture.py:close。
- 证据：finally 中 fixture.observe/dump 先于 fixture.close，前者异常可能跳过关闭；close 之后的 archive_manifest/verify_archive 又先于 shutil 清理，归档异常可能留下私有文件并缺少最终 lifecycle。新三次生命周期实际 PASS，本轮没有发现残留。
- 影响：异常证据保留与资源关闭不能互相牺牲；非正常退出时仍需明确 orphan 检查。
- 最小建议：仅把已有归档和资源关闭拆成嵌套 try/finally、保留 cleanup 错误；本地注入归档失败验证关闭执行。无需服务编排改造。
- 阻塞 V1：否，对当前成功归档证据；应在日常 Runner 使用前修。

### F13 — 安全范围与 Evidence 格式在两 Suite 中不统一

- 文件：business_verifier.py:78–85；remaining_verifier.py:24；environment.py:observe。
- 证据：Workspace tool_scope 只评成功/deduplicated；Fixture 对除 denied 外的所有未知工具尝试判安全失败。Workspace 原料检查主要 file_name/status/version/content，未比较所有 folder/ACL/配置域；T05 不检查全部工具范围。当前注册工具限制降低风险，但不是完整无副作用 Oracle。
- 影响：相同越界尝试在两个 Suite 可得不同结论；扩展工具后可能出现未观测的副作用。
- 最小建议：明确允许/禁止尝试与派发的统一安全语义，观察合同声明的可写业务域；不要承诺全平台副作用隔离。必要字段类型/事件身份做最小验证，不做通用 DSL。
- 阻塞 V1：否，限定现工具/业务域时；若发布统一严格 Scope 安全指标则需先对齐。

## OPTIONAL

### F14 — Dashboard / Artifact 上传服务

- 证据：artifacts.py 只有本地 JSON，现报告和原始文件已可读。
- 影响：浏览便利性，不影响本地确定性验收。
- 建议：需求明确后再做。阻塞 V1：**否**。

### F15 — 并行 Runner 与大规模显著性分析

- 证据：baseline.py 串行、每 Case n=3；当前无显著性宣称。
- 影响：吞吐和统计精度；不能拿此代替 Oracle 修复。
- 建议：先稳定协议，再按研究问题扩大样本。阻塞 V1：**否**。

### F16 — 自动任务生成、通用 DSL、插件市场

- 证据：VerifierSpec 仅固定 field_equality_v1，两个有限业务 Verifier 足够现有任务。
- 影响：未来扩展便利；当前不缺通用表达式能力。
- 建议：不纳入 V1 必要工程。阻塞 V1：**否**。

### F17 — 第二 Agent 的真实跑分或排行榜

- 证据：当前只有 Hexgent 执行入口，未独立验证其他 Agent。
- 影响：可移植性需要将来验证，但成绩不是当前交付目标。
- 建议：F07 可先通过薄 Adapter 合同与离线一致性验证收口；第二实现只在独立授权后接入。阻塞 V1：**否**，不得宣称已验证跨 Agent 可比性。
