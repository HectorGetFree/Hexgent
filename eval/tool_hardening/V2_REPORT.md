# Tool Real-Workload V2 正式结果

## 证据

- 已审核计划：`V2_SOURCE_MANIFEST.json`，未重新生成/修改。
- 全部计时样本及 durable Session：`runs/v2-20261008-approved-r2/report.json` 与其 sessions 目录。
- 独立非计时实际 Handler 探针：`runs/v2-20261008-profiles/report.json`，含原报告 SHA-256 引用。
- 早期 `v2-20261008-approved` 在首个预热遇到 Eval Session 未记录 query 的初始化错误，保留失败报告；没有成功计时样本被筛除。修复仅在 Eval，计划和正式工具不变。

## 执行边界

每题一对预热、二十对正式测量，交替 Serial/Parallel 顺序。共六十对、120 个完整任务、480 个批次、3280 次正式计时调用。预热与独立探针不计入性能统计。

Serial 使用正式 enabled=False，Parallel 使用 enabled=True/max_workers=4；默认 ToolResultLimits、真实 Handler、权限规则与正式 Pipeline 未修改。每个 variant 独立 durable Session 和 Context，任务内保留 Context 跨批状态。没有 sleep、Handler 替换、任意 shell 或模型 API。

完整任务时间包含四个顺序批次、durable 工具事实/结果外置以及每批输出正文核验，不含 Session/Context 初始化、源文件哈希扫描、报告序列化、独立 profiler。批次时间仅覆盖 Production execute_tool_batch，故四批贡献之和小于任务总量；剩余部分为 Oracle/循环推进，不能称为模型推理耗时。

所有结果路径使用真实保存的 Artifact，按 callId 解引用正文比较。逐调用核对 Tool Name/Arguments/成功状态、正文 SHA-256、输出顺序、最终结果集合，核对 permission allow 序号先于 dispatch，每次实际 dispatch 一次。前后扫描 Manifest 中全部文件/Discovery 集合及全部正式 Python 源码哈希。最终无源码变化或无效计时样本。

## 完整任务结果

| 任务 | 调用 / 批次 | Serial P50 / P95 ms | Parallel P50 / P95 ms | 配对中位加速比 | 配对平均降幅 | min / max speedup |
|---|---:|---:|---:|---:|---:|---:|
| Tool Pipeline | 26 / 4 | 35.329 / 45.453 | 37.159 / 43.573 | 0.944x | -4.92% | 0.789 / 1.128 |
| Recovery / Checkpoint | 25 / 4 | 31.708 / 59.140 | 35.060 / 47.567 | 0.922x | -6.84% | 0.812 / 1.412 |
| Memory / Context | 31 / 4 | 47.444 / 50.543 | 50.186 / 59.050 | 0.951x | -7.71% | 0.581 / 1.396 |

speedup_i = serial_total_i / parallel_total_i；表中是二十个配对加速比的中位数，平均降幅为 mean(1-parallel_total_i/serial_total_i)。没有使用批次加速比平均替代。负降幅表示并行更慢。全部原始任务/批次耗时保存在 JSON。P95 nearest-rank ceil(.95*20)=第十九个有序样本；不是稳定服务 SLO 或独立主机统计。

## 批次结果与贡献

S/P contribution 为每个样本 batch_ms/task_ms 的平均值，不是中位数之比。P95 亦逐批保存在原报告 summary.batches。

| 任务 / 批次 | 调用 | S P50 ms | P P50 ms | 配对中位加速比 | S/P 平均任务贡献 |
|---|---:|---:|---:|---:|---:|
| Tool / Discovery | 4 | 1.244 | 1.731 | 0.764x | 3.65% / 4.53% |
| Tool / Core | 4 | 3.427 | 3.891 | 0.875x | 9.56% / 10.31% |
| Tool / Dependencies | 12 | 19.940 | 20.671 | 0.961x | 55.63% / 54.96% |
| Tool / Tests | 6 | 5.739 | 6.256 | 0.941x | 17.83% / 17.73% |
| Recovery / Discovery | 4 | 1.290 | 1.815 | 0.705x | 3.94% / 5.27% |
| Recovery / Core | 5 | 4.644 | 5.246 | 0.875x | 14.78% / 15.41% |
| Recovery / Dependencies | 11 | 16.870 | 18.044 | 0.953x | 52.39% / 51.06% |
| Recovery / Tests | 5 | 3.856 | 4.327 | 0.882x | 11.93% / 12.66% |
| Memory / Discovery | 4 | 1.300 | 1.777 | 0.755x | 3.31% / 3.53% |
| Memory / Core | 6 | 14.714 | 15.241 | 0.965x | 31.43% / 30.46% |
| Memory / Dependencies | 14 | 17.614 | 18.457 | 0.946x | 36.63% / 37.57% |
| Memory / Tests | 7 | 7.970 | 8.590 | 0.929x | 16.85% / 17.07% |

## 实际并发与正确性

独立 profiler 观察真实注册函数的 call/return，不包裹或替换 Handler。每批 Handler entries 与调用数严格一致。

| 任务 | Serial 四批实际峰值 | Parallel 四批实际峰值 | 计时配对输出等价 |
|---|---|---|---|
| Tool | 1 / 1 / 1 / 1 | 2 / 4 / 4 / 4 | 20/20 PASS |
| Recovery | 1 / 1 / 1 / 1 | 3 / 4 / 4 / 4 | 20/20 PASS |
| Memory | 1 / 1 / 1 / 1 | 4 / 4 / 4 / 4 | 20/20 PASS |

峰值来自独立 instrumented runs，不是计时样本逐次峰值。原计时 runner 中探针调用最初遗漏 profile=True，造成空 profiles；已修正 Eval，另生成带原报告哈希的探针补充证据，不覆盖、不重跑六十对测量。空探针阶段另有每任务一对非计时执行，未纳入任何性能平均。

## 分析与 V1 区别

1. 更大批次的相对开销更小：Discovery 加速比约 .705–.764，而 11–14 个调用的 Dependencies 约 .946–.961；是描述性观察，任务内容、文件大小、结果持久化成本不同，不能称为隔离后的摊薄因果证明。
2. 本次读取约 270KB/345KB/278KB（含测试），没有额外扩展文件或重复读取。未进行大小控制实验，不能从三点建立文件大小—并发收益规律。
3. glob 单独成 Discovery，read_file 在其余批次；没有 glob/read 混合批次的独立因果证据。
4. 四批保留依赖屏障，批间没有重叠。Dependencies 在 Tool/Recovery 占约一半任务时间，Memory 则分散在 Core/Dependencies/Tests；不可用单批最佳值代替完整任务收益。
5. OS 热缓存、本地读很短；线程池、Python CPU/GIL、输出预算/落盘、Oracle 都可能限制收益。没有 CPU/I/O 分解证据，不能认定磁盘是主要瓶颈。
6. 原十三组固定等待微基准 2.61x/57.71% 证明等待重叠；V1 五调用小规模真实工具实验不含 Session 持久化；本次多批、durable Session 与 Oracle 均不同，三者不能合并求平均或宣称性能优化前后对照。

## 可用于简历的表述

构建工具并发与结果治理评测；基于工具、恢复、记忆上下文三类源码审计轨迹，完成六十对多批次串并行对照、3280 次真实工具调用，验证权限先于派发、结果内容与 Call ID 一致及持久化输出引用正确，并识别本地热缓存工作负载下的并行开销边界。

本次三题均无稳定正向耗时收益，不能写“真实源码审计提速 XX%”。FakeModel/固定计划与读取测试源码不等于 Agent 自主审计成功或对应测试全部通过。

## 回归与变更范围

- V2 合同/统计回归：3 tests，0 failures/errors/skipped。
- 全量 Eval：104 tests，0 failures/errors/skipped。
- 全量 Runtime：245 tests，0 failures/errors/skipped。
- 本轮只新增/修改独立 Eval 文件与输出；Production 哈希与审核 Manifest 哈希校验通过。原 session_restore.py / test_session_recovery.py 修改为本轮前已有内容，未覆盖。
- 原 Module V1 / 真实工具 V1 Artifact 保留；无模型请求、无 commit/tag。
