# Benchmark V1 最小收口路线

当前决策 **NOT_READY**。这是一份后续建议，本轮只读，不自动执行任何步骤。原 B3 模块消融取消，不进入 B4，不以 Hexgent 通过率作为验收条件。

## 1. 冻结发布范围与规则变更方式

明确当前 12 个定义、10 个可执行 Case；T05 多身份、T08 的 Hexgent unsupported 不算平台已测覆盖。T03 当前是小资料两阶段重算，不能标成长时压力。T04 eligible / T07 approved 作为相同刺激的两个归属或别名，不能重复加权覆盖。

所有后续 Oracle/公开约束/Verifier/协议修改使用新版本；保留 B1/B2 Manifest、Trial Records、Usage、FAIL/INVALID。变化后的结果不得无条件与旧分数组合。先规定哪些是错误统计纠正、哪些是任务定义变化。对应 F06/F09/F10。

验收：发布范围、unsupported、版本关联、Task/Protocol 状态定义写清；不需要新 Case、模型运行或架构改造。

## 2. 修正最小行为 Oracle 与公开任务一致性

优先 F01：未知提交后的核验必须先于后续风险写；拒绝后不得重复目标写调用；要求匹配目标订单/操作的写后核验。只检查必要前置关系，允许无关的合法工具编排变化，不规定固定整体顺序。审批记录与真实业务 write_attempt 的 call_id/参数/范围严格对齐。

并处理 F06：公开声明严格版本/写次数协议，或在独立业务语义论证后版本化合法版本链验收；不根据某个 Agent 的失败选择较宽松规则。F08 可随此将 Outcome 未完成和真实安全违规拆开，保持总体失败条件严格。

验收：当前 PASS 正例保留，盲重发、重复 denied、缺写后读、错误目标读、额外真实副作用均有反例；合法替代路径按公开合同得到一致结果。全部离线，不需要模型。

## 3. 补齐证据终态完整性

F05：归档后严格解析 JSONL、身份、序号、完成事件与派发/结果配对，必要 Evidence 给 digest；派生 tool audit 和权威账本/原始事件一致。运行中只有末尾半行允许暂缺，历史坏行不能忽略。

验收：缺失、损坏、截断、错 Session、不可归属派发/审批分别为明确 INCONCLUSIVE/INVALID；明确已有业务 FAIL 不被诊断 Trace 缺失掩盖。Verifier 保持只读，不写业务状态。

## 4. 对齐 Trial 预算与终止分类

F02/F03：在现 Eval 边界增加 Trial 累计真实派发/重试预算，Run/Session 不重置；跨阶段传递剩余预算或在派发前拦截。Token 是完成请求可观测总额，未报告部分 UNKNOWN；墙钟为 Trial 全程，Agent Run 时长另记。

冻结原因映射：Agent 有效执行但未完成/耗尽预算为 Task FAIL；Provider/环境不可用 INVALID；关键证据不足 INCONCLUSIVE；不支持特定能力属于 eligibility/unsupported。保留 partial 状态，失败不自动替补到成功。

验收：两 Run 累计超工具限额被检测；同一终止原因在 Workspace/Fixture/Memory 三入口分类一致；429 停止；多 Session Usage 不重复、重试不是新 Trial。不改 Production Runtime。

## 5. 对齐跨 Session 信息来源与薄 Adapter 边界

F04 首先解决 T05 旁路：B 不能重新取得专门用于续接验收的原资料；定义允许的持久信息与独立会话边界，保持业务事实 Oracle。Hexgent remember/ACTIVE/Memory injection 可保留为实现证据或专属 profile，通用验收不能要求其他 Agent 使用同名机制。

随后 F07：只把已有入口的执行、停止、继续、工具/审批/Usage/Session 观察归一到小合同；记录 Agent ID/Adapter 版本，HexgentConfig 一致性检查归 Adapter 私有配置审核。SQL/MinIO/SQLite 业务观察保持原独立权威来源，其他 Agent 不需要复制 Hexgent 内部事件。T08 语义为同一逻辑执行在既定切点继续，私有 Checkpoint 格式仅 Adapter 自证。

验收：仅用本地假 driver 和规范化 Evidence 检查接口即可，不需要第二个真实 Agent 或重跑 Provider。不能仅通过改“scenario”为 True 消除机制限制；必须保留信息边界和恢复连续性的可信证据。

## 6. 小范围维护收口

F10/F11/F12/F13：统一 active Inventory 和报告版本、INVALID 补测 lineage/上限、从计划算数量；固定可获取镜像 digest 或明确有限复现范围；归档失败仍关闭 Fixture；对齐两 Suite 的 Scope 语义和已观测副作用域。

验收：补测不能选择性替换 FAIL，纠正统计不能回写旧历史；资源归属及异常关闭有离线测试；成本覆盖声明与实际字段一致。避免通用平台、上传服务、插件系统或大量 Runtime 重构。

## 7. V1 Freeze Gate

- F01–F06 已解决，或对应能力明确从通用评分范围退出且相关覆盖承诺同步删除；不能把可信度缺陷仅写成免责声明后继续同口径评分。
- 支持公开合同的合法路径；安全边界明确；Evidence 终态可审计；Agent 失败不被排除；Trial 预算不随 Session/Run 划分变化。
- 若宣称可独立接入其他 Agent，完成 F07 的最小规范化边界；尚无第二 Adapter 的实测可列 limitation，不宣传已验证跨 Agent 可比性。
- 小规模固定任务族、T03 非长时压力、多身份/恢复 unsupported、外部服务输出不确定、未采集完整计费成本可以作为明确 V1 局限；无需为这些局限修 Hexgent Production。
- 只在发布范围确实要求新增覆盖时增加 Case：必要的长时业务刺激、完整多身份泄露 Oracle 或中性恢复协议；目前无必须新增 Case 的证据，先修正确性。

满足上述必要条件后可判断 READY_WITH_LIMITATIONS；完整扩展覆盖不是 READY 的隐含要求。Dashboard、并行、大规模统计、自动任务生成、DSL、模型排行榜和旧消融均不在最小收口路径。
