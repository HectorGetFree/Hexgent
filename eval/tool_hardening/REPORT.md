# Tool Real-Workload Hardening

最终证据：`runs/real-tools-20261008-final-evidence/report.json`。早期 v1/v2 是 Eval 接口适配失败的空目录，v3/v4/final 是开发核验，不混入最终样本。原 Module V1 Artifact 未覆盖。

## 方法

仅使用正式注册的 `read_file(path)` 与 `glob(pattern)`。每题四次不同源码完整读取及一次对应目录枚举，共五次调用。参数序列、源码 SHA-256、全部配对原始时间均在 JSON 中。工具源码、Recovery/Checkpoint、Memory/Context 三组互相独立，不代表 Agent 自主审计能力。

Serial 使用正式 `ToolParallelLimits(enabled=False)`；Parallel 使用正式默认四 workers。每题预热一对、测量二十对，交替顺序，无 sleep、无 Handler 替换、无丢弃慢样本。每批重新创建工具缓存，OS 文件缓存不清理。因此是本机热缓存源码读取，不是冷磁盘/远程 I/O。60 对共 600 个计时调用；预热 30 个调用、非计时 profiler 30 个调用另列。

计时覆盖正式 Tool Batch，不覆盖模型与 Session 持久化。完整 Loop 正确性使用正常持久化与默认输出外置策略，另行统计。profile 只观察正式函数进入/退出，单独执行，不污染计时；每次测量的 peak 没有伪造为 profiler 观测值。

P95 为 nearest-rank ceil(.95*20)，即每 variant 第二十个有序样本中的第十九项。二十次是顺序重复，不能当作独立主机、独立冷缓存或稳定服务尾延迟。中位加速比为二十个 S/P 的中位数，平均降幅为二十个 (1-P/S) 的算术平均；不是两个中位耗时相除后再换算。

## 结果

| 任务 | Serial P50/P95 ms | Parallel P50/P95 ms | 配对中位加速比 | 平均耗时降幅 |
|---|---:|---:|---:|---:|
| Tool Pipeline | 1.085 / 1.351 | 1.602 / 1.731 | 0.688x | -45.59% |
| Recovery / Checkpoint | 0.954 / 0.994 | 1.412 / 1.994 | 0.663x | -57.31% |
| Memory / Context | 0.953 / 1.086 | 1.403 / 1.528 | 0.666x | -48.11% |

每组 min/max speedup、全部四十个耗时、非计时并发峰值、结果内容均见原始 JSON。本地工具很短，线程池创建/调度与同步开销可能超过重叠收益；这是合理解释，不是隔离开销后的因果测量。不宣称真实工具性能提升。

原十三组固定等待微基准的 2.61x / 57.71% 仍只适用于其原工作负载，不与本表平均。

## Correctness / Coverage

| 能力 | 证据 | 结果 / 边界 |
|---|---|---|
| 真实源码串并行内容与 callId | 三组 × 二十对 | PASS；包含完整读取正文校验 |
| 并发峰值与真实 Handler 进入 | 三组独立非计时 profile | PASS；每 variant 五次真实 Handler |
| 完整 Agent Loop 下一轮模型请求 | 三组独立 FakeModel run | PASS；不是模型理解或自主任务成功率 |
| 乱序完成、有序交付 | 保存实际 completion order 与请求顺序 | PASS；不人为制造完成顺序 |
| Transcript / Finished / 请求内容 | 六项 checks / 每组 | PASS；外置结果原文保存在 resolved_model_request、finished_content_snapshot |
| 未授权 Dispatch | 三组 permission facts 对照 dispatch | PASS；只覆盖这些运行 |
| 读写串行屏障与写后可见性 | 真实内置 read_file/write_file，临时文件 | PASS；before / after 正文及实际请求一致 |
| 超时后的迟到 Handler | 独立 Eval Event 工具，两个 Handler | PASS；两次进入、两次返回，超时提交后 Transcript 不变，无迟到正文注入 |
| 既有安全体系 | Module V1 六场景 + 四十二测试 | 保留历史结果，不重定义为四十八类场景 |

迟到工具仅用于超时边界，不替代任何真实性能 Handler；走正常 ask→allow 的权限入口，不放宽策略。超时不终止正在运行的线程，不证明副作用 exactly-once。后台线程 join 完成后再核验 durable 结果。

## 验证

- 新 Eval 回归：3 tests，0 failures/errors/skipped，首项含三组 subtests。
- Eval 全量：101 tests，0 failures/errors/skipped。
- Runtime 全量：245 tests，0 failures/errors/skipped。
- 无真实模型调用；Production Runtime 与冻结 Artifact 无本轮修改。既有 session_restore.py / test_session_recovery.py 工作区修改保留。

## 简历建议

构建工具并发调度与结果治理；在源码、恢复与记忆上下文三类真实内置工具工作负载下，完成六十对串并行测量及三组完整 Agent Loop 验证，确认调用结果配对、持久化交付、读写屏障与超时迟到结果隔离，明确毫秒级本地读取的并行开销边界。

不得附加“真实工具提速 XX%”；本次未测得收益。固定等待微基准只能单列并标注适用范围。
