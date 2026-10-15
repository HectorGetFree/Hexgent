# B3-F2：执行协议与证据完整性修复

## 阶段结论

**READY_FOR_B4_VALIDATION（离线验收通过）**。本轮完成 Benchmark 内部协议 v2 与证据完整性检查；未运行真实模型、Provider、Docker 或 HexWorkspace API，未开始 B4。此结论表示可以进入受控验证阶段，不表示新版真实 E2E 已通过或 V1 可以冻结。

Hexgent HEAD：`adcf30ddb6958c6ed7e317db3066bdebef67a924`，分支 `main`。已有 B1–F1 未提交内容保留；未提交本轮变更。HexWorkspace 有其他工作产生的未提交修改，本轮未编辑该项目。

## 1. 修复映射与文件

| Finding | 实际问题与修复 | 主要文件 |
|---|---|---|
| F02 | 原预算不能保证多 Run / Session 共用工具配额；共享加锁 Trial 账本，在实际 handler 派发前阻断 | protocol_v2.py、eval_runtime.py |
| F03 | Agent 未完成与协议无效混淆；显式分离 Protocol、Task、Safety、Termination | protocol_v2.py、evidence_v2.py、baseline.py |
| F05 | 终态 JSONL、身份、配对及来源缺少严格验证；建立原始文件清单与独立复核 | evidence_v2.py、business_smoke.py、verifier_v2.py |
| F08 | 没有目标写入时，默认 CAS / side-effect 断言可能被解释为安全失败；依据实际更新与副作用判定，Safety 可为未知 | verifier_v2.py、protocol_v2.py |
| F10 | 补测与统计版本边界；仅 INVALID 可申请新 ID 替补，禁止有效 FAIL 替补；计划数量由 eligible Case 推导 | protocol_v2.py、baseline.py、各 smoke 入口 |
| 异常清理 | 观察 / 归档异常可能跳过清理；清理动作独立执行，资源关闭不依赖评分成功 | trial_finalize.py、business_fixture.py |

新增模块：`protocol_v2.py`（预算、分类、补测）、`eval_runtime.py`（仅 Eval 进程内接线）、`evidence_v2.py`（封存与复核）、`trial_finalize.py`（异常收尾）、`test_protocol_v2.py`（29 项测试）。

接线修改：`business_environment.py`、`business_smoke.py`、`memory_business_smoke.py`、`fixture_agent.py`、`fixture_smoke.py`、`business_fixture.py`、`baseline.py`、`verifier_v2.py`。未修改 Production Runtime 文件，未修改冻结 Contract / Oracle；F1 评分 Revision 保留。

## 2. 协议与版本

Task / Trial 外层 schema_version 仍为既有 1；F1 verifier_version 为 2。执行协议显式使用 `run_config.protocol_version=2`，账本与 Evidence Manifest 使用 schema_version 2，结果文件为 `protocol-result-v2.json` / `verifier-result-v2.json`。旧版本仍走旧入口。

F1 Baseline 必须找到同 Case 新版 Protocol Smoke，缺少时拒绝启动，不能借用旧 B2 Smoke。Eval Runtime 要求控制文件明确选择协议 2；预算身份、起始时刻与配置在下一进程不得重置。

## 3. 预算计数与终止语义

- `max_tool_calls` 单位：实际 handler 派发尝试，重试每次计数。共享文件锁账本覆盖所有 Run / Session，先记账再派发；记账失败不能继续业务调用。
- Kernel denied、非法工具名 / 参数、候选派发事件与实际派发分别统计。原生 `tool_execution_dispatched` 是候选事件，不能直接当作真实业务派发计数。
- 达到工具限额仍允许模型给出最终答案；下一次 handler 派发被阻止并记录 `trial_tool_dispatches`。
- Provider 预算覆盖经 Eval SDK 包装的 Main 与 Auxiliary 请求，关闭 SDK 隐式重试；使用共享已知成本与未完成请求的估算预留，调用前收紧 output cap。完成后保存原始 Usage，再累计实际 input + output。缺失 Usage 保留 UNKNOWN 并停止后续请求，不当作零。
- Token 预留使用本地输入估算，不能保证 Provider 实际账单绝不超限；完成后的已知超限会阻止下一请求。这是精确成本预付不可观测的边界，不等于工具预算事后检查。
- Wall Time 使用同一 Trial 起点，Runner deadline 与 Runtime 请求 / 派发闸门共同约束；资源关闭和归档可能在截止时间之后完成，不能将其解释为新增 Agent 预算。
- 超预算终止在环境和证据可信时是有效任务失败；如果独立 Oracle 已证明业务成功，不仅因终止标记而推翻成功。

## 4. Protocol / Task / Safety 分类

| 情况 | Protocol | Task | 说明 |
|---|---|---|---|
| 有效环境中业务成功 / 未达成 | PASS | PASS / FAIL | 由确定性 Oracle 决定 |
| Agent 自主停止、执行失败、预算耗尽 | PASS | FAIL；已独立证明成功则 PASS | 不排除有效失败 |
| Provider 429 / model_cooldown 或不可用 | INVALID | INVALID | 保存原始成本未知范围，停止队列 |
| 预期工具故障被触发，Agent 处理失败 | PASS | FAIL | Fixture 注入故障不是基础设施无效 |
| Agent 未触发故障且已明确失败 | PASS | FAIL | 未触发刺激单独记录 |
| 环境 / 注入器失效 | INVALID | INVALID | 实验条件不可信 |
| 必要证据缺失、Usage 不可确认 | INCONCLUSIVE | INCONCLUSIVE | 不猜测 PASS |
| 损坏、身份错配、摘要冲突等证据不可信 | INVALID | INVALID | 与单纯缺失区分 |
| 当前实现不支持 | BLOCKED | INCONCLUSIVE | 不进入有效任务分母 |

Safety 来源为独立 Safety 断言；没有可用安全断言时为未知，不能把 Task FAIL 自动标记为安全违规。Scenario 是否触发与 Injector 是否正常独立记录。

## 5. 原始证据与来源

证据层次：原始 Transcript / SDK 账本 / 生命周期及业务观察 → 规范化 Tool Audit / 业务 Evidence → Verifier Result → 聚合报告。聚合报告不证明原始事件。

`evidence-manifest-v2.json` 固定 Trial、Case、Contract 身份与各文件 SHA-256；Trial Record 引用其摘要和 Protocol Result 摘要。校验包括：

- 文件存在、JSON / 终态 JSONL 合法、顺序字段、Run / Session 身份和开始结束配对。
- 工具 start / finish 关联，规范化 Tool Audit 与原始事件一致；非法模型参数作为执行失败保存，而不是伪装成损坏 JSONL。
- SDK 请求开始结束关联，账本顺序、真实派发与原生候选事件对应，累计预算不超工具限额。
- Contract、Transcript、Run Report、Usage 引用必须在清单内；可选引用摘要必须匹配。
- 业务 Evidence 的 Trial 身份和必需字段，生命周期销毁结果与清理错误。
- 被 Runner 强制停止的未闭合原生 Run，仅接受同 Trial、可信 supervisor 来源且确认进程停止的独立记录；不伪造原生结束事件。没有该证据的缺失结束拒绝接受。

SHA-256 证明文件与引用一致，不能单独证明业务事件真实发生。可信来源是 Eval 派发 / SDK 边界、SessionStore 原始事件、环境管理器及独立业务观察；这是本地受信执行环境，不提供对恶意被测进程伪造整个目录的密码学防护。

正式评分在封存后重新读取合同与 Evidence；协议 v2 的直接 Verifier 调用也先检查完整性。缺少完整性清单不能 PASS。

## 6. 补测与清理

仅 INVALID 允许显式 `--replacement-of`：新 Trial ID，同 Case 和 Contract Hash，记录原结果摘要、来源、原因和 ordinal。默认每个原 INVALID 在同 campaign 最多 1 次替补，替补不能继续形成无限链；注册表防止重复申请。有效 FAIL 不得替补。Provider 不可用后停止队列，不自动无限重试，也不切换模型。该上限是 campaign 范围，跨 campaign 的实验治理仍需保留并审查 lineage。

Workspace：停止属于 Trial 的 Runtime，独立执行合同保存、权威观察、归档、账本保存与 Destroy；中间失败不跳过 Destroy。

Fixture：停止子进程、quiesce 服务并等待已接收 worker、保存最终观察、close、archive、删除私有临时目录分别执行。服务启动失败关闭已创建 socket / DB。清理失败被记录并使协议无效，不能声称资源已成功销毁。

## 7. 离线验收

执行命令：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest eval.benchmark_v1.test_protocol_v2
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s eval/benchmark_v1 -t . -p 'test_*.py'
git diff --check
```

结果：新增测试 **29 / 29 PASS**，完整 Benchmark 离线测试 **129 / 129 PASS**，无失败，diff 检查通过。

覆盖：两 Run / 两 Session 累计预算、真实 handler 前阻断、denied 与派发区别、Token / Wall / UNKNOWN、Agent 与 Provider / Injector 分类、损坏 JSONL、缺少结束、调用结果不配对、串 Trial、摘要错配、未封存引用、补测 lineage 与禁止 FAIL 替补、Fixture / Workspace 异常收尾、部分初始化失败、正式评分和版本隔离。外部依赖使用 Mock；临时 Fixture 测试不调用真实模型和业务服务。

## 8. 历史保全

修复前后对 `eval/benchmark_v1/runs/` 与 `hexgent/`（排除 __pycache__）所有文件按路径排序，组合路径与各文件 SHA-256：

- 文件数：**8,367**，前后相同。
- 组合 SHA-256：`783c4fb49a5cb084341afb4ebd821b458b37626eb30924704f28c75f695963cc`，前后相同。
- `git diff -- hexgent` 无 Production 差异。
- 对原 B2 的 30 次 Trial 与补测 3 次 Trial 的实际 TrialManifest 只读校验：33 个唯一 Trial 均为 `LEGACY_NOT_AUDITABLE_UNDER_V2`。它们没有新版完整性清单，不能冒充 v2 审计通过，也没有被改判为任务失败。

历史 29 PASS / 1 FAIL / 3 INVALID 保持原统计语义；本轮不产生新版真实 Benchmark 分数。未原地修改历史 Manifest、Contract、Oracle、Evidence 或 Usage。

## 9. 未解决与下一阶段边界

- 新版真实 Agent E2E 尚未验证；必须先获授权执行新版 Protocol Smoke，再验证 Baseline，不可复用旧 Smoke。
- T05 multiidentity 与 T08 执行阻塞保持；本轮不修改 Production Memory / Restore。
- 精确 Provider Token 输入只能响应后得知；中断 / 失败请求成本可能未知。HexWorkspace 外部 Embedding 成本不在 Eval SDK 包装范围内，明确保持不可观测。
- 清理的真实 Docker / 服务故障边界只有离线 Mock 验证；不能宣称已经通过真实资源残留检查。
- 不提供跨 Agent 通用 Adapter、全局补测注册服务或外部签名审计。这些未列入本轮实现范围。

无新增阻止进入 B4 受控验证的离线缺陷；上述真实运行与成本观测边界必须在 B4 报告保留。本轮在 F2 报告后停止。
