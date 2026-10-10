# M3 C5-B P0 Fix Report

2026-10-10；只执行确定性模型，无付费请求。结果：C5-B 局部修复验收 PASS；可进入下一阶段审批，不自动执行真实模型。

## 根因与最小修复

Checkpoint 的 relevant_context 是已召回正文缓存。Restore 重放 Store 正确，但 Continue 直接恢复缓存，Builder 将已 superseded 的 A 再次注入。

Production 修改仅四个文件，57行新增/3行删除（不含新增测试）：

| 位置 | 修改 |
| --- | --- |
| hexgent/session/session.py::reselect_context_records | 使用当前 active_records、原词法 Retriever、原 limits/fallback重新选择；结果改变时沿用已有 retrieval事件，不新增状态系统/磁盘重载 |
| hexgent/agent_engine.py::_build_run_state | 正式 Continue 按当前最新用户Query重新选择Record；保留原 Memory复核，不改其生命周期 |
| hexgent/loop/agent_turn.py::run_turn | 预算组装前刷新缓存；每次发送前复用原 Builder/注入预算重新组装，更新后续Checkpoint所用状态 |
| hexgent/loop/call_model.py::call_model | 可选prepare_request回调在实际发送及Runtime空流重试前执行；无回调的辅助调用保持原逻辑；Memory request_guard仍在最终发送外层 |

不修改 Checkpoint schema/已有文件，不清空全部记录、不新增Session/submit、不用Prompt绕过旧事实，不修改历史或Summary。
当前有效新事实B通过原Retrieval规则正常参与选择；不是仅过滤A。

## R1–R8

| 要求 | 结果 | 实际证据 |
| --- | --- | --- |
| R1 C5-B，A排除/B可注入 | PASS | test_r1_corrected_checkpoint_selects_b + 原始Gate复测 |
| R2 C5-A保持事实且无重复条目 | PASS | test_r2_unchanged_checkpoint_retains_a_once；请求block恰好一条A |
| R3 A→B→C | PASS | test_r3_chained_supersede_only_c；正式Continue仅C，来源与active Store对应 |
| R4 后续多次请求不复活 | PASS | test_r4_multiple_requests_do_not_revive_a；三个实际create均仅B |
| R5 空流重试前更正 | PASS | test_r5_empty_stream_retry_reselects_after_correction；首请求A→空流期间更正→重试请求B |
| R6 enabled=false | PASS | test_r6_continue_disabled_does_not_inject_cached_record；存在快照A并更正B，OFF仍无Record区块 |
| R7 ON/OFF原Gate | PASS | gate-20261010T111518Z/isolation.json，11/11 true |
| R8 M1/M2及Runtime | PASS | Runtime 276/276；相关Eval 42/42；M2原有17项在Runtime内，不重复计数 |
| 非空Summary隔离 | PASS | test_nonempty_summary_on_off_retains_normal_history；真实Compaction函数+确定性摘要响应，ON/OFF input完全一致、OFF摘要仍含历史A，只有ON有Record区块 |

7项新增 unittest 均通过。Failures/Errors/Skipped均0；git diff --check通过。
FakeModel固定响应仅提供轨迹；正确性由Record区块、Store状态、来源和正式continue进行外部断言，不由模型声称完成判断。Fake usage不作为成本数据。

## Artifact与故障分类

- 原始失败结果仍保留：eval/memory_m3/runs/gate-20261010T110550Z/report.json。
- 原始Gate不改Oracle，新复测：eval/memory_m3/runs/gate-20261010T111518Z/report.json。C2/C5-A/C5-B/ISOLATION均PASS。
- 最终新增回归证据：eval/memory_m3/runs/fix-regression-final-20261010T111906Z/，每方法有完整真实Session目录及oracle-evidence.json。
- 每方法保存缓存Checkpoint（适用时）、当前Store、最终create请求/Record区块；Session的完整Transcript包含restore、continue和retrieval事件。Summary实验非恢复契约，Checkpoint为null，明确N/A。
- STALE_CONTEXT_CACHE：Checkpoint保留旧A属正常历史缓存，不再被当权威。
- STALE_RECORD_INJECTION：R1/R3/R4/R5重试后的相关请求均无已替代条目。
- RETRIEVAL_SELECTION_FAILURE：eligible B/C均被选择，关闭开关为空。
- RESTORE_STATE_MISMATCH：同Session正式Continue可达，block ID/来源与当前active Store对照。
- EVAL_ISOLATION_FAILURE：原11项隔离及非空Summary对照均未发现。

Gate report的production_unchanged=true仅表示复测运行前后源码未变化，不表示本轮没有批准的Production diff。M1/M2原始runs hash前后仍一致；没有覆盖历史工件。

## 残余风险与Go/No-Go

GO：本轮批准的局部修复和确定性门槛通过，具备申请有限真实模型Smoke条件；真实模型仍须另行批准。

边界：

1. 不新增工具来源自动Supersede，仍为CAPABILITY_GAP；用户显式更正路径C2仍PASS。
2. 不解决合法quote与错误正文、重复旧正文重新active等先前审计项；若下一阶段要宣称完整来源可信性或永久撤销，仍须分别验收。
3. 仅约束Record注入渠道；正常历史、非空Summary仍可包含旧事实，不能宣称模型遗忘。
4. 请求复核依赖当前Engine的Session内存权威，复用其单Run/安全边界约束；不支持绕过Engine直接并发改Session、多Engine同时维护同一Session或外部编辑投影。
5. Runtime空流重试已验证；SDK内部传输重试不是独立Runtime观测点，本轮不宣称逐SDK重试的新状态复核或网络可靠性。
6. 每轮及发送前重新选择会使新active候选按原规则进入选择，不增加状态/冲突识别；预算仍由原Builder裁剪。未量化性能收益/Token节省。
7. Summary仅用于隔离正确性，无M4压缩优化或真实摘要质量结论。

未修改M2 Store/工具/生命周期测试；未commit/tag。完成后停止等待人工审核。
