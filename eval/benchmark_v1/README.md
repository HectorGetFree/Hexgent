# Benchmark V1 — B1-A

本模块只定义任务合同、Trial Artifact 与确定性 Verifier。没有 Runner、真实任务、环境重建或 Provider 调用。使用项目已有 Pydantic 和 unittest，不导入 Production Runtime。

## 合同边界

`TaskContract.model_validate_json(...)` 严格解析版本 1，拒绝未知字段、非法枚举、非正预算、重复断言、缺失 Oracle 引用。至少一项 Outcome 和一项 Safety 必须存在。Outcome 只使用独立业务状态。

只有 `contract.agent_visible()` 可以交给 Agent：包含 `user_query`、`allowed_tools`、`execution_budget`。整个 `scenario_config`、`expected_outcome` 和 `verifier_spec` 都是 Evaluator-only。不得把完整合同或 Trial 目录直接挂载到 Agent 工作目录。Query 是作者主动公开的文字；投影不是通用敏感信息过滤器，作者不得在 Query 中嵌入隐藏答案。

预算单位是累计 Provider 输入＋输出 Token、真实工具派发尝试数、Trial 墙钟秒数；跨 Session 累计，工具重试不新建 Trial。包含压缩／提取 Provider 用量，不把未知用量当作零。本轮定义预算语义，不执行限额。

Full 与 Ablation 共用完全相同的 Task Contract / digest；模块开关只放 `TrialManifest.run_config`。`baseline_id` 标识环境数据基线，和配置中的 Full/Ablation variant 不是同一个概念。模型、Harness 配置、消融开关由后续调用方写入配置快照。一次 Protocol Smoke 与三次 Baseline 分别分配四个独立 Trial ID；本轮不实现次数调度。

Pydantic 冻结属性不意味着嵌套 JSON 深度不可变。保存冻结合同文件和 SHA-256；验证时重新计算摘要，拒绝合同被修改后沿用旧 Trial。

## Trial 目录约定

```text
artifacts/<suite_id>/<case_id>/<trial_id>/    # Evaluator-only
  contract.json                            # 冻结 Task Contract
  trial.json                               # TrialManifest，包含配置和代码版本
  observations/environment.json
  observations/business-state.json
  observations/tool-audit.json
  sessions/<session_id>/
    transcript.jsonl
    reports/run-001.json                    # 可包含多个 Run Report
    provider-usage.jsonl                    # 可选，用显式空引用表示未采集
    checkpoints/...                        # 可选，原格式保留
  verifier-result.json                     # 也可嵌入 trial.json
```

所有引用相对于当前 Trial 根目录，不允许跳出目录；Session 文件必须位于对应 `sessions/<session_id>/`。后续收集器复制或引用原有 Transcript JSONL、Run Report schema v2、Checkpoint schema v2，不重新定义 Runtime Event。Session-local `run-001` 必须和 Session ID 一起定位。Provider Usage 可以引用原 Transcript 中含 Usage 的文件，也可以引用以后独立采集的 Usage 文件；仅定义引用，不实现采集和聚合。

`TrialManifest.sessions` 支持多个 Session 和每个 Session 多份 Report；不把模型请求或重试当作新 Trial。代码版本映射可记录两个仓库的 commit 和工作区摘要；本模块不调用 Git。合同 digest 是规范 JSON 内容摘要，ArtifactReference.sha256 是文件原始字节摘要，两者不要混用。

`artifacts.write_json/read_json` 仅用于本地结构化数据读写，不提供上传服务。Verifier 只读取，不调用写盘函数。不检查未来 B1-B 环境是否已经重建。

## Verifier 输入与结果

`VerificationInput` 接收合同、Trial Manifest、Artifact 根目录，以及标明 Suite/Case/Trial 身份的独立观测。每个业务字段或工具审计字段都显式附证据引用；环境就绪也必须附证据。`Observation` 必须由受信任的独立观察器产生，不得从 Agent 的自然语言完成声明构造。

`FieldEqualityVerifier` 只比较顶层字段和 `expected_outcome[expected_key]`，比较区分布尔与数字类型，不提供表达式、插件或顺序匹配。证据文件要存在、位于 Trial 内，并在提供文件摘要时匹配摘要。B1-A 不解析证据文件的业务语义，也不能认证观察器的真实性；观察器的独立性、字段来源与完整性由 B1-B/B1-C 落实。

| 状态 | 语义 |
|---|---|
| PASS | 环境、合同与审计文件可核对；全部声明的断言通过 |
| FAIL | 有证据的 Outcome 或 Safety 明确失败，即使其他证据缺失也保留业务失败 |
| INVALID | 已证实环境无效，或合同／身份／协议不一致 |
| INCONCLUSIVE | 关键证据缺失、环境有效性未知，或业务通过但声明的归因 Trace 条件不满足 |

已证实协议／环境失效优先于业务评分。业务失败优先于缺失 Trace；Trace 不会把失败业务改为成功。默认没有任何 Compaction / Memory Trace 通过要求；只有具体实验合同显式声明时才核对。Trace 条件不满足不记作 Agent 业务失败。

Verifier 不调用 LLM、不依赖固定工具顺序、不使用最终自评、不修改业务状态。结果记录每项断言、证据、失败分类和 Verifier 版本。JSON 反序列化仍校验身份与状态分类。

## 离线验收

```sh
.venv/bin/python -m unittest eval.benchmark_v1.test_contracts -v
```

`examples/local_contract.json` 是纯本地合同示例，`fixture_update` 没有注册或执行，不对应 T01–T08。`local_agent_visible.json` 和 `local_verifier_result.json` 展示该合同的投影和临时离线 Fixture 产生的验证结果；不是真实 Benchmark 分数。证据路径是临时 Fixture 的相对路径，正式验收执行上面的测试，测试会重新构造文件。

B1-B 可直接使用 baseline_id、Trial ID 和目录合同建立独立环境并产出 readiness 观测。B1-C 可以在同一接口上定义具体业务断言或实现 `DeterministicVerifier`，无须修改 Runtime。本轮到此停止。
