# 最终因果判断

## Q1：首次分叉在哪里？

第一轮模型输出与工具参数，在执行任何工具之前。ON/OFF第一和第三个Glob pattern不同，OFF还返回reasoning条目。证据：两侧provider-calls.json /0/response/output、/0/tool_calls；FIRST_DIVERGENCE_REPORT.md。Production先call_model后execute_tool_batch：agent_turn.py:382,550。

## Q2：分叉前输入是否一致？

捕获的request完全相同，包括System/User/工具schema/模型参数；不是已验证HTTP wire bytes或Provider内部状态相同。首次后续input分叉在主轮2。证据：ROUND_ALIGNMENT.json /0、/1逐字段比较与哈希；agent_turn.py:362-385。

## Q3：416.75秒差额主要在哪？

主Provider观测时间差 **401.555590秒，占96.35%**；工具profiler差 **0.714879秒**；辅助差约0.000114秒；账面剩余差 **14.479510秒**不能冒充已观测Context/持久化时间。OFF首轮相同输入已慢约19秒，终态响应慢约83.66秒，其他轮次也普遍较慢。请求数、Token与规划轨迹均不同。证据：LATENCY_ATTRIBUTION.md；ON/OFF_TIMELINE.json summary和每请求原始索引；v3_smoke.py:105-139及v3_provider.py:124-130。

## Q4：多少可直接归因调度？

原Agent差额中的正向Scheduler因果收益 **无法识别（UNDETERMINED）**，不是声称真实作用绝对为0。可直接对照的是同轨迹20对重放：ON Trace中位S/P0.9609×、平均降幅−4.87%；OFF Trace0.9621×、−3.49%。两侧并行均略慢，不能把其毫秒差额直接代入原Agent的profiler计时。证据：`/Users/hector/dev/hexgent/eval/tool_hardening/runs/v3-cross-trace-20261008-r1/report.json`及各trace/report.json；scheduler.py:233。

## Q5：Scheduler改变模型可见输入的证据？

没有本次相同调用集因调度改变正文、Call ID配对、权限或交付顺序的证据。动态workspace/session引用确实可见，但来自Eval隔离。共享cache、超时及取消存在潜在路径，当前没有证据表明它们导致本次规划分叉。证据：SCHEDULER_PATH_AUDIT.md、MATCHED_TOOL_RESULTS.json、同轨迹100%等价报告；scheduler.py:598及context_manager.py:92。

## Q6：支持并行提升Agent端到端性能吗？

**不支持。**只能说不同轨迹的两次Run观察到约4.12×差异，主要差额位于Provider请求；不能说并行让Agent加速4.12×。首轮输出分叉早于调度，单对非同轨迹运行无法隔离Provider波动、规划和上下文差异。任务语义还待人工审核，OFF严格引用FAIL也应保留，不能将工具PASS包装为完整任务PASS。

## Q7：还缺什么最小实验？

如果问题仅为Scheduler对相同工具轨迹的作用，当前Cross-Trace Replay已给出因果证据，无需再用不同Agent轨迹替代。

如果要主张Agent端到端平均收益：预先固定任务、源码、Provider配置、工具权限和endpoint，执行多对随机/交替顺序的ON/OFF，保留全部样本、Provider故障及任务质量判定，报告分布和不确定性；当前1对不够。为了拆解剩余壁钟，再增加独立单调时钟request/stream-close/context/persist/handler spans及SDK attempts。Provider支持seed只能减少随机性，不能保证相同轨迹；不能通过过滤成“同样漂亮的轨迹”选择性制造收益。本轮不执行这些实验。

## 交付与限制

ON_TIMELINE.json、OFF_TIMELINE.json包含完整Provider请求和durable事件身份/时间/原始证据位置；无阶段独立时间戳的标为UNOBSERVED。EVIDENCE_MATRIX.json区分直接因果、观测贡献、合理机制和不支持项，并保存原始输入文件SHA-256。ROUND_ALIGNMENT.json、MATCHED_TOOL_RESULTS.json提供补充逐轮/逐调用对齐。

只读审计：不调用Provider、不重跑模型/Benchmark/测试、不改Production/Eval Harness/Oracle/旧Artifact，仅新增本目录脚本和报告。不commit/tag。原Artifact哈希已在分析末尾重新核验。

补充核验：两侧原workspace以及当前hexgent/tests源码逐文件SHA-256均符合原manifest.source_hashes，无差异；原始6份artifact/provider/transcript输入文件哈希保持不变。两侧全部tool_execution_finished.attempt_count均为1，ContextRecord retrieval均为空。git diff --check退出码0；没有将已有session_restore.py/test_session_recovery.py工作区改动清理或覆盖。
