# Pinned Constraints Deterministic Gate

证据类型：**EVAL_SIMULATION**。结果：PASS。建议 GO，仅进入下一轮真实模型实验审批，不表示 Production 已具备该能力。

## 实现与原始证据

- Runner：`gate.py`；离线回归：`test_gate.py`。
- 冻结方案：`runs/gate-final/manifest.json`。
- 汇总：`runs/gate-final/report.json`。
- 每场景 `constraints.json` 保留来源序号、原文、active/superseded 和替代关系。
- 每侧 `evidence.json` 保存原始请求、包装后请求、实际 FakeModel 请求、Token 估算、预算拒绝原因、Transcript delta、工具结果、终态和恢复前状态。
- `stage-a/.hexgent/sessions/` 和克隆工作区保留未改写的 Production Transcript/Checkpoint。
- `comparison.json` 保存完整请求隔离结果。仅归一化克隆工作区路径；不忽略工具参数、调用 ID、历史或模型参数。

约束 A：禁止修改原始资料，仅允许读取并回复。
用户显式更正 B：撤销只读要求，仅允许写 result.json，原始资料仍不能修改。
这是一张预注册显式事件映射，不具备自动识别能力，不使用 Extraction 或 Memory。

## 结果

| 场景 | Source | Active Set | Request | Summary | Resume | Budget | Behavior / Execution | Isolation |
|---|---|---|---|---|---|---|---|---|
| E1 早期禁令与继续 | PASS | PASS | PASS | N/A | N/A | PASS | PASS | PASS |
| E2_KEEP 摘要保留 | PASS | PASS | PASS | 保留 | N/A | PASS | PASS | PASS |
| E2_OMIT 摘要遗漏 | PASS | PASS | PASS | **遗漏** | N/A | PASS | PASS | PASS |
| E3 用户更正 | PASS | PASS | PASS：仅 B | N/A | N/A | PASS | PASS | PASS |
| E4_A 无更正正式恢复 | PASS | PASS | PASS | N/A | PASS | PASS | PASS | PASS |
| E4_B 更正后正式恢复 | PASS | PASS | PASS：仅 B | N/A | PASS | PASS | PASS | PASS |
| E5 完整请求预算不足 | PASS | PASS | 完整候选；未发送 | N/A | N/A | PASS：明确拒绝 | PASS：无派发/副作用 | PASS：首请求可比 |

E2 的 Summary Oracle PASS 表示预注册的保留/遗漏条件被正确复现，不表示遗漏摘要保留了约束。
E5 Behavior PASS 是无派发、无副作用，不是任务完成；其终态为 failed 且保留预算原因。
E4 使用原 Session ID 的 `AgentEngine.restore → RunRequest.continue_run()`，没有新 Query。
E4 同时通过真实 create_plan / update_plan 工具检验 Plan 保存、恢复及完成。

## Baseline 与模拟能力的区别

- E1：两侧正常历史均包含 A；没有证明模拟区在短历史下改善行为。
- E2_KEEP：旧用户消息不再原文存在，但摘要保留 A；Pinned 仍独立注入 A。
- E2_OMIT：Baseline 的原始主请求 input 不再包含 A，摘要也遗漏 A；Pinned 最终 instructions 完整包含 A。
- E3/E4_B：历史仍可含 A 与 B，Pinned 有效区仅包含 B，A 的来源及 superseded_by 留在审计工件中。
- 所有 Baseline 均没有独立保护区，标记 `CAPABILITY_GAP: NO_INDEPENDENT_PINNED_CONSTRAINTS`；不是 Production 工具管线失败。

因此 PASS 表示确定性对照正确实现并验证预注册契约，不表示 Baseline 已保护所有约束。

## 隔离与预算

7 个子场景各验证：相同 Stage A 文件快照、恢复前 Transcript、恢复后的活动 Context/Plan/权限/配置/Memory/Record；请求去掉新增区块后完全一致。
两种 Compaction 调用使用真实 Production 压缩函数和相同固定摘要，完整调用参数一致。
Restore 各自追加带时间戳的恢复审计事件；这些不是初始快照差异，未改写日志或 Checkpoint。

E5 预注册预算政策：完整未包装输入估算 + 实际 max_output_tokens + 1 Token 余量；两侧相同。
包装后再次估算 instructions + input + tools，并计入输出预留；不检查单纯区块长度。
预算失败在 FakeModel.create 前抛出 `EVAL_PINNED_CONTEXT_BUDGET_EXCEEDED`，完整约束候选保存、不截断。
E5 Baseline 2 次实际主请求，Pinned 0 次；仅比较首个候选原始请求，不把后续缺失强行视为相同。
这是 Eval 层容量模拟政策，不是生产模型窗口配置。

## 成本

- A 区块：每个主请求增加约 **71 estimated tokens**。
- B 区块：每个主请求增加约 **81 estimated tokens**。
- E1–E4 每侧各 2 次主请求；Pinned 无额外主模型请求。
- E2 每侧各 1 次固定摘要请求；没有因保护区新增摘要请求。
- E5 拒绝候选增加约 71 tokens，但没有实际发送。
- Provider Token / latency / 模型遵守率：NOT_AVAILABLE；付费模型调用 0 次。

## 验证与历史

- 新增 Eval 回归：6/6 PASS，包括完整七子场景运行、预算零派发、来源错配拒绝、失效项过滤、摘要请求透明性。
- Runtime 回归：276/276 PASS。
- 现有 Memory/Module/Context Eval 回归：42/42 PASS。
- `git diff --check` PASS；run 工件被现有 gitignore 规则忽略。
- 初轮 `gate-initial` 保留：Eval 比较误把 Restore 时间戳当初始差异、E4 固定工具调用漏必填参数，产生 FAIL。
- 修正的是 Eval 接线与更严格的执行 Oracle；没有修改约束 Fixture、生产代码或降低判定标准。`gate-v2` 和最终 `gate-final` 独立保留。
- Production 哈希与前置一致，M1/M2 run 哈希一致；没有 commit/tag。

## Go / No-Go

**GO，等待审批后进入最小真实模型对照；暂不批准 Production 实现。**
现有证据已证明：在摘要遗漏早期规则时，独立保护区可以避免请求层的信息缺失，更正与正式恢复也可保持有效集合。
未证明：模型行为提升、自动约束识别、外部更正竞争处理或自动可信事实提取。
下一阶段仍应保持同一资料/模型/权限，独立记录请求级保留与实际行为，不能把 FakeModel 的固定遵守轨迹当成增量价值。
