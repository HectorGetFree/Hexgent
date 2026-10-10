# M3 Evaluation Plan — 待审核，不是验收结果

## 人工批准补充（2026-10-10）

仅 Phase 1 Eval-only Deterministic Gate 已批准，真实模型和 Production 修复未批准。
C2 仅用户显式更正；自动工具冲突替代未实现则 CAPABILITY_GAP。
C5-A 为无更正正式恢复一致性；C5-B 为 Checkpoint 后更正、正式继续不得复活旧事实，分开 Oracle。
禁止改写 Checkpoint、新 Session 或新 submit 替代 Continue。
ON/OFF 必须先证明同 Stage A 快照/Store/Memory/Transcript/权限/input，唯一目标差异为 Record开关，实际请求OFF不注入、历史/summary/回读仍保留。发现 P0 即停止待审批。

基线审计：`eval/MEMORY_M3.md`。仓库 `/Users/hector/dev/hexgent`。
本文件只设计，不创建 Runner/Fixture，不执行测试或付费模型，不修改 M2。

## 1. 实验原则

分开三种证据：确定性机制验证、真实 extraction qualification、真实最终使用。
真实模型记录不能由 fixture 注入补救。提取失败/无合格记录仍保留：PROVIDER_FAILURE、EXTRACTION_REJECTED、NO_QUALIFIED_RECORD 分开，不悄悄换样本。
新 query 必须在同 Session；正式恢复使用同 Session 的安全 Checkpoint + Restore→RunRequest.continue_run。跨 Session 仅用于 C6 隔离负例。
ContextRecord 不能预装完整任务答案来制造效率收益：早期记录保存资料中的事实/约束，后续任务要求组合多个依据形成结构结果。

## 2. C1–C6 可执行性和预注册设计

| 场景 | Fixture / 步骤 / 独立 Ground Truth | 当前可执行性 | Oracle |
| --- | --- | --- | --- |
| C1 关键事实跨步骤 | research fixture 含唯一合成服务的地区、配额、提交限制；真实 read_file 获取后经历多轮其他资料工作，同 Session 后续查询完成 JSON 决策 | 可执行；同 Run 和下一 submit 分开报告。无 steering 的同 Run 自动更新不作为已实现保证 | 来源逐字定位+实体/值绑定；记录、selection、实际注入、最终结构值分别判断 |
| C2 冲突 Supersede | 项目配置 A→B，先实际读取 A，用户明确更正并授权 B，再读取 B；同 Session 继续问当前配置 | 用户更正版本可执行；纯工具 B 自动 Supersede 当前不符合规则，记 CAPABILITY_GAP，而非改 parser | B active/A superseded、变更事实和来源合法；请求 Record 区块无 A；答案使用 B。历史 A 单列 |
| C3 来源可信性 | 固定成功工具事实、用户陈述、纯 assistant 推断、失败工具、截断尾部；另外合法 quote 配错误实体/值的反例 | 正常类型/quote 门槛可测；content entailment、用户授权语义硬校验有缺口 | 外部 oracle 对源事件类型/status、逐字 quote、正文结构值、授权动作分别验证；有来源但不被支持仍 FAIL |
| C4 真实 Compaction 后保留 | C1 记录形成后读自然业务长文和无关资料，真实 Production compaction 路径，随后同 Session查询。长文不得重复 filler，无固定 60-token 摘要 | 已有 ContextLimits/RunOptions 可触发；实际 applied 事件为门槛。只有摘要未切到源轮不算压缩后验证 | Record 有效集前后一致、完整 Transcript 来源仍在、源轮已离开 active_messages、actual Record 注入和答案分层 |
| C5 安全恢复 | A 已召回并进入安全 paused Checkpoint；同 Session 安全边界显式更正 A→B，Restore→Continue；另设无更正 control | 正式接口可达；新 Store B 与快照 A 不一致是重点待复现风险。不得新 submit 替代 continue | 恢复 Store 对 Transcript replay一致；正式 continue可达；每个实际请求只注入有效 Record；旧 ID/正文/来源不复活 |
| C6 Session/Memory 边界 | Session A 自动形成记录，不调用 remember；同工作目录新 Session B 查询唯一实体；单独检查已有 Memory 内容不变 | 可执行；生产自动 Memory helper 未接线 | A 有合格记录；B Record为空、Memory无新增、请求无目标注入；独立底层隔离，不以最终拒答代替 |

C2/C5 的机制子实验允许安全 Engine API upsert，但必须独立标记 TRUSTED_API_EVIDENCE，不能作为自动 extraction 成功。
最初每类一个确定性主场景及必要边界子例；规模是设计提案，不是已执行数量。不要把断言数当场景数。

## 3. ON/OFF：唯一差异与两层口径

使用现有 `ContextRetrievalLimits.enabled`；无新增 Production 开关。

### 同 Session 后续 submit 配对（当前最可靠）

1. 在隔离 fixture 中真实 Stage A，通过 Production extraction 形成记录。
2. qualification 检查 active、真实来源、完整实体/取值绑定、对 Stage B 合理相关；不能只用 token_overlap。
3. 冻结完整 Stage A workspace + .hexgent + Transcript + Context projection +工具结果+已发生 Compaction；记录源码/config/fixture hash。
4. 克隆两个完全相同快照，均 Restore 相同原 Session，然后新 submit 相同 Stage B query。
5. ON enabled=true；OFF enabled=false。相同 Memory 状态，最好空且 retrieval 同时固定 disabled；不是一侧关 Memory、一侧开。
6. 两侧的 extraction/Compaction、工具权限、模型、预算均相同；OFF 不关其他 Agent 能力。不能删除 OFF Transcript 或禁止仅 OFF 回读资料。

区别于 M1：OFF 仍可能从历史、summary、工具重读获得事实，这是 ContextRecord 在正常 Session 中的合法 baseline，不是隔离失败。必须记录各个渠道，不能为了制造 Lift 只移除 OFF 的历史事实。
Stage A snapshot 的 qualification 通过率必须按全部预注册案例报告；不能只展示通过 gate 的样本。成对 Stage B 是条件样本，单列分母。

### 正式 Continue 配对（不能只切 enabled）

Checkpoint 缓存 relevant_context；continue 不重新调用 Retriever，仅切 config.enabled=false 不能保证关闭已缓存注入。
第一选择：把 C5 留作恢复正确性验收，不计算 ON/OFF Lift。
若未来确需恢复配对，可 Eval 层明示只清除 OFF 的 recalled tuple、保留其余完全相同 State，并作隔离等价测试；这属于 EVAL_INJECTION_ABLATION，不宣称 Production OFF 模式。未经审核不实施。
同 Run 变体也需证明开关真正作用于请求，不能借给模型额外 steering 或提示制造 ON 优势。

## 4. 独立 Oracle 与采集

| 层次 | Artifact | PASS 条件 |
| --- | --- | --- |
| 来源 | 源文件 hash、真实 read_file call/result、Transcript 序号与外置正文 | 真实观察到的原文完整支持实体、数值、限定条件；非仅相关词 |
| 提取 | extraction 最终请求/响应、batch 预览、parse/commit事件、active Store快照 | 真 Production路径，quote合法且独立语义 oracle合格，未补写记录 |
| 替代 | 原新 ID/状态、完整 mutation事件、用户更正来源 | 明确授权范围、A失效/B有效；相同文本重提交的规则独立检查 |
| Selection | context_records_retrieved +每请求组装前候选 | active集合/预算/相关性正确；不等同最终注入 |
| 注入 | 透明SDK最终create参数、request_type、span_id、request hash | 解析 relevant-session-context；ID/正文/category/sources与当前有效投影一致；旧值注入数为0 |
| 其他渠道 | active input、summary、Memory block、工具回读 | 标记目标事实实际来自哪里；旧summary事实不混计Record注入，但影响最终正确性 |
| 最终使用 | 用户可见产物/严格结构JSON | 外部 Ground Truth确切匹配，不用模型自评、completed不等于任务成功 |
| 恢复 | checkpoint校验、run_state、恢复Store、continuation transcript | 同Session正式continue；有效集合与完整durable facts一致，不仅两次restore输出相等 |
| Provider | 每请求类型/时间/状态/真实usage、失败事件 | 缺失=null；主模型、extraction、compaction分别统计，SDK内部重试可观测性限制单列 |

恢复请求的旧 facts 比较只约束 Record 注入区块，不承诺清除历史或让模型遗忘。Record与Memory冲突同时注入需单列，不靠最终答案掩盖。
失败归因：STORE_OR_REPLAY_CORRECTNESS、STALE_INJECTION、SOURCE_VALIDATION_GAP、EXTRACTION_FAILURE、MODEL_REASONING_FAILURE、PROVIDER_FAILURE、EVAL_ISOLATION_FAILURE、CONTINUE_BLOCKED。

## 5. 指标：预注册分母

- Extraction Qualification Rate = 合格记录的预注册案例数 / 所有预注册 Stage A案例数。
- Critical Fact Retention Rate 分两层：有效Store保留事实数/应保留事实数；最终请求中有效注入事实数/该检查点要求注入事实数。不得合并。
- Stale Fact Injection Count = 含任一已 superseded Record ID或对应旧正文的实际Record区块请求数；另报条目出现次数，避免混淆计数单位。
- Grounded Answer Accuracy = 独立答案正确且被有效事实支持的案例数/预注册案例数；再单列有效Provider执行分母。
- Restore Consistency = Store与durable事实有效集/来源/状态一致的恢复案例数/所有预注册恢复案例数；另报实际continued请求一致率。
- Provider Input Tokens：主/提取/压缩分别实测，全部Usage完整才汇总全成本；不完整只报可观测小计+缺失请求数量。
- ON−OFF Accuracy百分点差；配对tokens差、工具回读差仅观察。模型轨迹不同不可将全部延迟/轮次变化归因Record。

先两例真实Smoke（C1后续submit、C2用户更正），经过隔离/来源/请求采集gate再批准C4/C5。不在本轮决定大规模重复次数或宣传提升。

## 6. 阶段与停止条件

1. 审核本审计及契约：Session/Run、新工具来源更正、旧正文重新激活的预期。
2. Eval-only deterministic：先复现 P0、验证关闭开关/请求观测/正式continuation；保留真实结果，若Production gap影响评测就停止。
3. 单独批准最小 Production修复（如果复现证据要求），不碰M2生命周期，无自动权限/记忆扩展。
4. Production extraction 的真实Stage A qualification和两例Smoke；得到空记录/超时不靠API种子补救。
5. 再执行C4/C5和有限ON/OFF配对，报告全部注册与条件有效样本。
6. 人工确认后冻结M3；不自动进入M4。

风险：8秒提取超时可能使真实qualification受Provider限制；1,000字符预览导致源尾部不可见；当前同Run缓存/Continue快照/重复旧正文问题可能触发P0；摘要保留事实使OFF也正确，不应视为实验失败；真实小样本不支撑广泛稳定性。

本轮尚未实施上述实验，没有M3 PASS/FAIL数据。停止等待人工审核。
