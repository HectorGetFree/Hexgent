# B1-C 真实业务 Case 与 Protocol Smoke

本目录的 `cases/<task>/contract.json`、`baseline.json` 使用 Schema / Verifier 版本 1，当前 Case 分别为 T01 v1、T02 v2、T03 v3。Oracle 只在 Evaluator 的合同中，Runtime 镜像不包含 eval 目录，Runtime 仅挂载自己的 workdir，不能访问合同或 Oracle。Agent 只收到业务 Query、资料库内材料和后续公开要求。

## 三个 Case

| Case | 业务目标 | Baseline | 隐藏确定性 Oracle | 预算 |
|---|---|---|---|---|
| T01 | 综合立项、正式批准与报价作合规采购决策，处理9000元提案与正式批准口径差异，交付有精确来源的 JSON | 3 文档 | 批准12000元；Alpha/CN；总成本10500元、余量1500元；交付2026-11-06；三个来源及实际使用版本正确 | 200000 Provider Token / 35 工具尝试 / 360 秒 |
| T02 | 真实创建中间计划，再接收600元培训支持要求，更新原文件并保留早期约束 | 3 文档 | V1 draft：10500/1500；V2 approved：11100/900；保留团队、林舟、预算、交付日和地域；仅创建1次、更新1次 | 400000 / 60 / 720 秒 |
| T03 | 综合8份相互依赖材料形成中间方案；Alpha开通延至12月后改选合规可交付方案，重算排期与成本 | 8 文档 | V1 Alpha：34000/4000；V2 Gamma：37000/1000；开通11-18、迁移11-19、上线11-20；保留CN、30天、禁止身份导出、120席位、2000元培训、30分钟回滚和4项验收 | 600000 / 90 / 900 秒 |

任务不是工具流程脚本，Query 不指定调用顺序。T02/T03 通过已有 Workspace Conversation 的两次 StartRun 提交两个业务阶段；在第一次 Run 结束后观察真实中间状态，再公开补充条件。不会提前把新增条件注入初始 Prompt。每个 Trial 可包含一个 Session 多个 Run，沿用 B1-A 的多 Session 结构。

Case 修订保留在 `revisions/`：T03 v2 澄清保留期、回滚目标和预留开通时段的固定业务参数，避免阈值/自由排期与确定性 Oracle 不一致；T02 v2、T03 v3 提供公开目标文件夹 ID。`list_folder` 根目录返回 null，创建要求正整数父目录 ID，不能要求 Agent 猜测资源身份。目标 ID 每 Trial 由真实 Folder API 核验，不是隐藏答案，实际响应保存到 `public-business-context.json`。历史 T02 v1 的真实运行和失败结果不覆盖；业务 Oracle 和版本链要求没有放宽。

日期和金额是明确业务字段，交付物是 JSON 对象或单一 JSON 围栏。Verifier 不用关键词推断自由文本语义：数字/布尔类型敏感、字段集合严格，验收项数组允许顺序变化但拒绝重复项。原文叙述的价值在业务分析，确定性交付格式使结果可可靠核对。

## 验证证据

`BusinessCaseVerifier` 实现 B1-A 接口，按 Case 计算业务语义断言，再由 B1-A FieldEqualityVerifier 汇总结果。它只读取冻结合同和已归档的独立观察，不访问网络、不写盘、不调用模型。

- Outcome：T01 最终结构化交付物逐字段与事实 Oracle 比较，核对三个来源的 documentId/versionId 与实际读取/检索回执。T02/T03 从 MySQL 不可变版本记录及 MinIO 正式对象读取 V1/V2；正文摘要、阶段产物与最终语义都核对。
- Safety：原 Seed 的文件名、状态、版本和内容摘要不变；仅允许一个目标新文档、两版本、两次成功 mutation；更新 expectedVersionId 与 V1 主键和正式更新结果一致；实际成功工具必须属于合同白名单。read_file 只允许续读 Runtime offload 文件。
- Execution Evidence：Transcript / Tool Trace / Report、权限决定、Compaction 及其成本账本、Offload 与续读调用、Provider Usage。事件本身不能替代业务结果。

第二阶段未出现、中间稿未在第一阶段真实存在、错误 CAS、无关资料修改、额外创建/版本、错误事实/来源都会被拒绝。关键观察缺失或 mutation 状态未决不能默认 PASS。原始资料本身不是 Agent 输出，阶段 Observation 是独立读取结果。

## 环境与执行

`BusinessCaseEnvironment` 复用 B1-B 生命周期与资源归属检查。仅扩展固定 Baseline、只读 MySQL CLI utf8mb4 编码、正常 Provider 配置和 Runtime TOML；Production 不改。宿主 Provider DNS 需要已有代理时，仅映射宿主 loopback 到 host.docker.internal，内部服务明确 NO_PROXY，不新建代理服务。

Kernel 权限保持既有 default 模式；Smoke 通过现有 permission resolve 接口按合同工具名称允许或拒绝，不修改权限管线，也不打开 auto_approve。Workspace Gateway 的冻结 allowedTools 只包含该 Case 授权的真实业务工具。

Runtime 配置与当前生产仓库的 ContextRecord 默认关闭一致，Compaction 使用原64K主动阈值、128K窗口，Memory保持生产默认，工具并发关闭。没有人为降低阈值、重复材料、空转或伪造工具结果。

```sh
.venv/bin/python -m unittest \
  eval.benchmark_v1.test_contracts \
  eval.benchmark_v1.test_environment \
  eval.benchmark_v1.test_business_cases -q

.venv/bin/python -m eval.benchmark_v1.business_smoke \
  --case T01 --trial-id <独立TrialID> --root <新目录> \
  --workspace-source /Users/hector/dev/hex-workspace \
  --build-metadata <B1-B构建元数据> \
  --docker-socket unix:///Users/hector/.docker/run/docker.sock \
  --provider-env-file <私有Provider配置> \
  --embedding-env-file <私有Embedding配置>
```

单次入口只运行选择的 Case，不实现批量实验、三次 Baseline 或消融。Runtime 与 Workspace 使用 B1-B 的镜像 ID；已核对该 Runtime 镜像源码到当前 HEAD 的 Production 没有变化。

## 结果与归因

`protocol-result.json` 和 `verifier-result.json` 分开保存：有效任务失败可以是 Protocol PASS / Task FAIL，不能为追求 Task PASS 改 Oracle。环境或 Provider 不可用为 Protocol BLOCKED / Task INVALID；证据或实现错误不能声称 Protocol PASS。

`execution-observation.json` 单独记录 Compaction/Snapshot/Summary、offload 与 read_file 范围。没有 Compaction 就是观察条件未满足；发生 Compaction 也不自动成为增益样本，B1-C 没有配对消融。本次固定业务资料规模较小，后续需要更大但有业务含义的材料和阶段才能评估 Compaction 增益。

Provider Usage 来自真实 MAIN 完成事件和可获取的辅助账本，未知或未报告请求不估算。Seed/Index 的 Embedding adapter 未保留 usage，本轮明确标为不可获取；不把已知 Harness Token 之和宣称为全部 Provider 成本。预算跨阶段监控已知 Harness Token 和 Trial 墙钟，Runtime 也使用正常单 Run 限额；Embedding 和未报告重试的总成本预算不是完整强制上限。

Artifact 包含冻结合同/摘要、Baseline、配置、独立观察/版本正文、Transcript/Report、Tool Trace、Usage、Task/Protocol结果和Archive/Destroy事件。所有秘密只注入Trial私有环境文件，不写公开配置，销毁后删除。先前无效运行原样保留，不覆盖或混入有效 Smoke。

本阶段完成三个 Case 的可信协议后停止，不自动进入 B1-D、B2 或 B3。
