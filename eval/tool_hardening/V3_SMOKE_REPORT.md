# V3 Smoke — STOPPED_SMOKE_GATE

## 原始证据

- 成功 Preflight：`runs/v3-preflight-20261008T130507Z.json`（gpt-6.1-sol、usage_available=true）。
- Smoke 原报告：`runs/v3-smoke-20261008/report.json`。
- 首题 ON 原始 Artifact：`runs/v3-smoke-20261008/tool_pipeline/ON/artifact.json`，Provider 请求/响应另存 provider-calls.json，durable Transcript/结果工件保留在 workspace/.hexgent。
- 只读重投影：`runs/v3-smoke-20261008/audit-corrected.json`。没有覆盖原 Artifact。

只运行 Tool Pipeline ON 一次；Agent terminal=completed。由于 Usage Gate 未通过，Tool OFF、Recovery ON/OFF、Memory ON/OFF、轨迹重放全部 NOT_RUN。没有扩大样本或为追求收益重跑。

## 自主调用分布

十五个主任务响应，加一次 ContextRecord 辅助提取请求，严格分开统计。

| 主任务响应中的调用数 | 响应数量 |
|---:|---:|
| 0 | 1 |
| 1 | 9 |
| 3 | 2 |
| 7 | 1 |
| 8 | 2 |

- 主任务真实 Tool Calls：38，平均每响应 2.533；多调用响应 5/15=33.33%。
- 候选独立只读调用：29/38=76.32%，按同响应不同工具/参数统计，不跨轮合并。
- 实际并发响应/批次：5；Handler 峰值4；真实 Handler entries=38；Finished success=38。
- 注册表仅保留原 read_file/glob 对象，未替换 Handler，模型可见工具由正式工具过滤路径生成。没有提示模型输出并发调用或 V2 固定序列。
- 逐响应 response_id、工具名/参数、候选机会、峰值与耗时在 artifact.turns；Provider 第5次调用是辅助提取，不是零调用的主任务轮次。

已证明当前模型在该主题下自发产生多个工具调用，正式调度器实际执行了并发；不代表另外两题或其他模型的一般行为。

## 计时与 Usage

- 单次 instrumented Agent Run：354918.44ms（354.92s）。
- 所有 Provider 调用消费流时间：344504.29ms（344.50s）。
- 正式 Tool Batch generator resume 执行区间累计：615.28ms。
- 这些是带 profiler、Provider 观察和 Artifact 写入开销的 Smoke 诊断数字；不是无探针性能 benchmark，也不是 Provider 服务端纯推理耗时。
- Provider Usage 覆盖15/16次。已知部分 input=181565、output=3901；严格标 PARTIAL，不充当任务完整 Token 总量，不以 estimator 补齐。

第5次辅助提取未收到 completed/usage；durable `context_record_extraction_failed.error_type=ModelCallInterrupted`。Production ContextExtractionLimits.timeout_seconds=8，流观察约8041ms，证据支持辅助提取的运行时超时边界，不支持推断 Provider account/policy/model 故障。

另有 Eval 透明性缺口：Observer 当前没有转发 with_options，导致提取层不能按正式代码路径设置 scoped SDK max_retries=0/timeout=8；Runtime Timer 仍触发。不能将这一配置差异隐去并宣称 Provider 路径完全等价。下一轮应先修正 Eval 透明代理，再决定独立重跑；本轮不继续请求模型。

## Oracle

1. 成功读取关键模块：PASS。scheduler/execution/permission_pipeline 全部真实读取；还包括 retry、agent_turn 和四个测试文件。
2. 原 Oracle 将 resolved absolute path 与 relative workspace 比较，误报未读取。仅修正 Eval 路径处理；原始 Artifact 保留，独立重投影后 missing_required_reads=[]。
3. 输出结构包含四个问题、四项结论与15段证据；仅1/15为严格逐字匹配。其余存在缩进改写或省略号，不满足预先冻结的 quote contract，未放宽 Oracle。
4. 人工语义核对：相邻安全分组/不安全屏障、权限先于 dispatch、按原调用顺序交付、超时不强杀线程、未知副作用不自动重试等主要结论与源码方向一致。不能把引用格式失败直接解释为十四项语义幻觉；也不能因语义大致合理就宣布 Oracle PASS。尤其同一轮同时完成的 futures 迭代不能被扩大为严格全序完成时间证明。

结论：Agent terminal completed != Task Oracle PASS。本题机械引用契约 FAIL；完整任务资格/结论不能自动 PASS。

## Evidence Matrix

| 项目 | V3 证据 |
|---|---|
| 当前模型自然产生同轮多调用 | COVERED：首题ON，5/15响应 |
| Production 实际并发 | COVERED：5批，峰值4 |
| 全部三类任务自然调用分布 | PARTIAL：仅首题 |
| Tool ON/OFF Agent 对照 | MISSING：门槛停止 |
| 同轨迹重放调度因果收益 | MISSING：未启动 |
| 完整 Provider Token | PARTIAL：15/16请求 |
| 外部任务 Oracle | FAIL：逐字引用不满足；路径bug已独立更正 |
| 源码快照一致 | PASS：首题复制与执行后哈希一致 |

V1 等待微基准与 V2 固定调用轨迹仍保持原口径。V3 不能提供任何提速百分比，不能用先前确定性实验补齐真实模型 ON/OFF 缺失项。可用于事实描述：在一次真实模型源码审计中观察到38次成功工具调用、5个自然多调用响应及最多4路实际执行；不得推广为任务成功率或总体性能收益。

## 验证 / 停止

路径修正后的最终离线回归：Eval 104 tests、Runtime245 tests，0 failures/errors/skipped；git diff --check PASS。路径修正后仅对原数据执行 v3_audit 和离线回归，没有新增真实模型请求。Production 未修改；原用户工作区变更保留，旧报告未覆盖，无 commit/tag。本轮停止，等待审核是否先修复 Eval proxy/观测后重启 Smoke。
