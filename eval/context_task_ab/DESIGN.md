# Context Task A/B V1 — 真实任务设计（待审批）

状态：DESIGN_ONLY / NOT_RUN。没有新增Fixture、Runner或模型调用。本方案不继承旧C2的PASS；旧质量FAIL和INCOMPLETE保留。

## 1. 目标与证据边界

回答：真实模型自主完成自然、多步骤任务时，Production默认v2 Snapshot+Summary是否能在保持结果正确性的前提下减少累计上下文成本？
预置任务资料和独立Ground Truth是允许的；禁止预置Transcript、工具调用轨迹、模型回答、Snapshot或Summary。所有上下文必须由真实Production Agent Loop调用真实内置工具形成。资料是合成业务Fixture，不声称真实用户业务数据。
不再将源码阅读型62.55%作为目标值，不以增加填充、强制读取或事后调阈值制造收益。

## 2. 当前可复用组件（已核对代码，尚未实现本方案）

- `hexgent/agent_engine.py`：空Session、正式RunRequest.submit、Checkpoint Restore及RunRequest.continue_run。
- `hexgent/tool/tool_registry.py`与`hexgent/prompt.py`：实际注册的read_file、glob、list_directory、write_file、edit_file，以及create_plan/update_plan；不用shell、网络、Workspace服务工具。
- `hexgent/context/context_budget.py`：128000窗口、64000主动阈值、8000输出预留、8000安全余量、24000近期保留；128K依然不是Provider独立容量证明。
- `hexgent/context/key_fact_extractor.py`：默认v2，独立Schema/Source/预算校验。
- `hexgent/context/context_compaction.py`与SessionStore：完整轮次边界、候选验证、原子提交。
- `eval/phase5c/specifications.py / fixtures.py / artifacts.py`：可参考TaskSpecification、克隆和外部Oracle模式；原Fixture规模小，不直接复用为长任务，不修改冻结Phase5C。

当前没有真正的Production Compaction.enabled开关。不得把抬高阈值当作完全关闭压缩，尤其不能漏掉reactive分支。

## 3. 六个候选任务

以下只是任务合同，不是已生成数据。资料数量和长度是候选范围，不是强制工具调用数。不得把Oracle放入Agent可读工作区。

### IS1 — 供应方案比较与采购建议

- domain/category：资料综合 / 多文件决策。
- prompt：请根据工作区里的供应方案、评估要求和澄清资料，比较各方案的费用、交付条件和风险，给出推荐及备选。把依据整理到 outputs/comparison.json 和 outputs/recommendation.md；仅提出建议，不联系供应商或更改原资料。
- fixture：vendor-review；约20–28份独立报价、服务条款、预算规则、兼容性和澄清资料；正文以实质条款为主，无重复填充。
- 工作流：发现资料→区分报价/澄清版本→跨文档合并约束→比较可行性→输出可追溯建议。
- Oracle：逐方案约束与费用组成、推荐集合、条件性风险、来源路径/quote位置、输出结构与允许文件差异。Ground Truth由独立数据合同计算，不规定模型必须采用某个读取顺序。
- 避免shortcut：没有单一摘要文件提供全部答案；但允许模型高效导航、摘取和回读。

### IS2 — 事件复盘与证据时间线

- domain/category：资料综合 / 多来源时序与归因。
- prompt：整理这次合成服务事件的时间线，区分已证实事实、推测和未决问题，指出恢复完成的证据及仍需核验的事项。输出 outputs/timeline.json 和 outputs/review.md，不执行修复。
- fixture：incident-review；约18–24份不同角色记录、状态导出和独立时间段日志；存在时区、重复转述和后续纠正，不用重复相同日志凑长度。
- Oracle：规范化时间与因果先后、有效状态、旧结论撤销、允许的不确定性、每条关键结论引用。单纯重复日志不计独立事件。
- 边界：资料为合成事件，没有外部服务或真实事故；不把模型结论等同已执行恢复。

### RU1 — 多环境配置与修订合并

- domain/category：规则修订 / 结构化配置操作。
- prompt：依据基础配置、环境例外和当前生效修订，生成各环境的候选配置，并说明覆盖关系及不能直接执行的变更。写入 outputs/configs/ 和 outputs/change-review.json；不要修改输入配置或部署。
- fixture：configuration-review；约18–24份基础/区域配置、迁移说明、批准范围和修订记录；精确值分布于不同真实用途文件。
- Oracle：完整JSON Schema、字段优先级、单位/数值、有效修订、禁止的跨环境继承、变更授权范围和来源。
- 特别检查：相同字段不同环境不是矛盾；旧值出现在历史说明中不等于被当作当前值。

### RU2 — 分批申请审核与规则版本

- domain/category：规则修订 / 文档到结构化结果。
- prompt：按当前生效政策审核工作区里的申请材料，列出通过、拒绝和待补证据的申请，并给出适用规则和理由。生成 outputs/review.json；不得付款、联系申请人或修改材料。
- fixture：application-review；约20–30份申请与附件，另有政策、日期范围、例外授权和更正；不在单个表里预写审核结论。
- Oracle：申请分类、必需材料、日期/额度/例外条件、未知项不猜测、引用与允许差异。每个决定有独立事实组合，不靠重复模板制造规模。

### WF1 — 项目验收与条件性交接

- domain/category：状态性交接 / 多步骤项目工作流。
- prompt：根据各工作流状态、验收证据和批准记录，判断项目是否满足交接条件；生成 outputs/handoff.md 和 outputs/readiness.json，列出已完成、未完成、阻塞及带前置条件的下一步。不要发布或修改源状态文件。
- fixture：project-handoff；约20–28份工作流记录、验收材料、变更决定和权限批准；不同对象状态不能混成一个全局完成布尔值。
- Oracle：每阶段完成/未决状态、前置依赖、批准对象和版本、下一步条件、不发布边界。只检查交付物，不要求机械复述全部历史。
- 可选用户更正：仅在合同预先定义时于同一安全边界提供同一修订通知；两侧相同，不允许看结果后补提示。

### WF2 — 批次处理账本对账与接续建议

- domain/category：状态性交接 / 数据与记录核对。
- prompt：核对工作区里的批次输入、处理结果和恢复账本，列清已提交项、可重试项以及必须人工核验的项，生成 outputs/reconciliation.json 和 outputs/next-actions.md。不要重放任务、修改账本或连接数据库。
- fixture：batch-reconciliation；约18–26份分片输入、提交记录、状态变更和未知结果说明；可能存在重复投递及迟到记录，但不把计数当成副作用次数。
- Oracle：按稳定ID对账、已提交/未知窗口、不能自动重试的集合、缺证据保留未知、条件动作及来源。不得宣称Exactly-once。

## 4. 任务规模资格Gate

优先审计实质信息量，而不是预定调用数。资料候选规模约3–7万源正文估算Tokens，但读取多少由模型决定；完整请求还含工具Schema、Plan及历史结果。
禁止要求“必须读取所有文件”以触发压缩；Oracle可要求完整审核结果，模型可选择自己的有效证据路线。
若高效任务轨迹未达到默认64000阈值，记录NO_COMPACTION_OPPORTUNITY：不是失败，不增加无关文件、不降阈值补救。
若OFF超过完整硬预算，记录OFF_CONTEXT_LIMIT，不视为模型推理失败，不能据此宣称保持质量同时节省成本。
所有任务目前SOLVABILITY_NOT_VERIFIED、TRIGGER_NOT_VERIFIED；尚不能称qualified benchmark。

## 5. 两层实验（先做同轨迹分支，不同时扩大两层）

### 第一层：真实Stage A快照分支，优先Smoke

1. 空Session、真实模型、Production Agent Loop、真实内置工具自主完成任务的探索阶段。
2. 在预注册条件下截取第一次完整Tool Batch Checkpoint：下一次完整候选请求达到默认主动阈值，但仍在共同硬预算内；捕获必须在Production开始自动压缩前。
3. 关闭执行器并冻结Stage A工作区、Session Transcript、Checkpoint、Plan及缓存语义。不得手工编造已读取事实。
4. 将相同快照克隆为ON/OFF工作区，使用同Session ID和正式Restore→Continue。
5. ON执行原Production自动Compaction；OFF只在Eval子进程关闭Compaction，硬预算与其他能力不变。
6. 两侧继续由真实模型自主完成外部Oracle任务，不使用固定工具调用脚本。

如果Runtime实际Checkpoint事件时序无法在压缩前可靠截取，标记HARNESS_GATE_BLOCKED，不伪造Checkpoint或改用新Submit。
后续轨迹可以分叉，记录差异。相同起点降低探索噪声，但不能把所有行为差异都解释为调度/压缩纯成本差异。

### 第二层：整任务ON/OFF，后续单独审批

两侧分别从相同空Session资料克隆开始，自主执行整任务。用于产品级结果，不把工作路径差异全部归因于压缩。不要与第一层数据混合平均。

## 6. OFF隔离方案（仅设计，必须离线验收）

拟采用独立Eval子进程中的窄边界替换：仅替换Agent Turn调用的Compaction入口。硬预算内返回not_needed且不变更上下文；超预算明确停止，零Provider派发。主动与reactive调用均处理。ON进程完全不替换。
这不是Production配置开关，不创建Production feature flag，不在共享进程中同时Monkeypatch ON/OFF。
Builder、Token estimator、输出预留、最终请求检查、Tool/Permission、read_file复用、Plan均保持一致。Memory和ContextRecord关闭，两侧相同，Pinned不提供隐藏答案。
必须先通过FakeModel验证：入口调用覆盖、零辅助请求、无历史修改、相同硬预算、首请求隔离、恢复一致性、超预算零派发、跨进程隔离。未通过不得调用真实Provider。

## 7. 工具与安全

仅开放read_file、glob、list_directory、write_file、edit_file、create_plan/update_plan。实际名称/参数以当前注册表为准。
原资料只读，只有outputs/可写，其他写入拒绝；禁用bash、网络、包安装、数据库、外部服务、Workspace capability与Memory生命周期工具。
不同任务和分支用全新可信临时工作区。Hexgent不是OS sandbox；不把工具权限称为操作系统级隔离。
权限策略两侧相同。是否能在Eval层同时限制工具可见性与实际派发必须用现有Permission Pipeline离线验证，不能只靠Prompt。

## 8. 指标、分母与失败归因

主指标：task_oracle_pass、critical_fact_use_correctness、累计Provider输入/输出Tokens。ON包含MAIN、Extractor和全部Summary候选；共享Stage A单独报告，后续分支成本可比，端到端总成本也分别报告但不将共享阶段计成两次实际消费。
辅助：工具调用数、重复读取次数/完整内容注入、MAIN轮数、压缩次数、主/辅助延迟及总执行时间。内容复用不等于减少文件I/O。
按任务报告配对差值，再报告等权分组统计；质量与成本分开，失败样本不能只因成本小而“更高效”。只在两侧Oracle均PASS时报告完成任务的成本差异，同时保留全预注册结果。
Usage缺失保持null，不用估算补齐真实成本；可观测小计不冒充完整成本。
分类：MODEL_TASK_FAILURE、PROVIDER_FAILURE、TOOL_POLICY_DENIAL、EVAL_INFRA_FAILURE、OFF_CONTEXT_LIMIT、NO_COMPACTION_OPPORTUNITY、UNKNOWN。任务资格与单次模型成功分开。
Token指标不是货币成本；Provider路由价格和容量未独立验证。不做128K容量探测。

## 9. 执行阶段与预算草案（未批准、不是费用承诺）

P0：设计审批→实质资料Fixture与外部Oracle实现→离线隔离/克隆/权限/预算Gate。零付费调用。
P1：只选IS1进行一次共享Stage A+ON/OFF分支Smoke，不同时跑六题。若任务自然不触发或OFF不可执行，保留结论；不得调阈值、换Fixture后重跑至通过。
初始硬上限草案：Stage A最多18次MAIN、累计600000本地估算Tokens；ON/OFF各最多8次MAIN、累计400000本地估算Tokens；ON最多1次Extractor、1次Summary，均纳入分支Token总额。因此最多36次实际Provider尝试、总估算上限1400000 Tokens。
输出沿现有8000主输出/4000Extractor/2000Summary限制。调用前以完整请求+输出上限预扣估算额度；缺Usage或首个Provider失败停止，不重试、不额外Preflight探测；所有失败请求计入次数。
此预算是保守停止上限，不是实际消耗、货币金额或用户当前授权。达到轮次/成本上限则记BUDGET_STOP，不能偷偷增加收尾调用。当前Provider延迟与历史体积可能令Smoke昂贵，应在冻结实际Fixture后重新计算精确请求预算再审批。
P2：P1有效后，每题1对资格Smoke；确认三域任务/Oracle/隔离有效，才另行审批每题3对。不能直接套用“每题3对”承诺稳定性。

## 10. Artifact合同与Go/No-Go

保存TaskSpecification、Fixture/Oracle哈希、真实Stage A Transcript、工具/文件状态、安全Checkpoint、分支克隆校验、完整请求/响应、逐Provider Usage、权限/派发、输出差异、外部Oracle、成本账本和停止原因。
Model/Provider、参数、真实配置、代码Hash、输出预算、OFF执行profile均记录。首个分支请求只有预注册Compaction造成的上下文差异；Summary/Extractor是ON新增阶段，不假装两侧最终输入相同。

本轮结论：READY_FOR_TASK_DESIGN_REVIEW；NOT_READY_FOR_REAL_MODEL_RUN。
主要待核验：Fixture实质规模、任务可完成性、自然阈值可达性、压缩前Checkpoint截取、OFF完全隔离、文件写入权限、实际费用接受程度。
完成本文件后停止，不实现Fixture/Runner、不调用模型、不修改Production或冻结资产、不commit/tag/push。
