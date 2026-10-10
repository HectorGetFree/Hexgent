# V3 Smoke 执行契约

使用现有已通过的 gpt-6.1-sol Preflight；三题各一对 ON/OFF，不扩样本。Production Registry 在 Eval 作用域中收紧为原对象 read_file/glob；不替换 Handler，不变更 Permission 或模型调用参数。只读源码快照只含 hexgent/tests 的源文件，排除 .env、既有 .hexgent 和缓存。

任务提示、关键读取要求与四个问题在 v3_smoke.py 的 TASKS/prompt 中于真实执行前定义。没有注入 V2 序列，没有提示模型生成多调用。外部机械 Oracle 校验成功读取关键模块、四项结论与实际读取源码逐字引用。语义准确性和无依据结论必须人工审核，不以机械检查冒充完整 Oracle PASS。

Smoke ON/OFF 均使用同一 HexgentConfig，唯一实验开关为 tool_parallel.enabled；统一资源上限为 16 model rounds、100 tool calls、600s Loop budget。模型参数由 Production Loop 当前路径决定，SDK retry/timeout 沿用当前 Provider client；不额外自动重跑任务。

Observer 原样透传 Provider 事件，保存 response id、完整 response、工具参数、usage、异常与流耗时。实际 Handler 峰值和 Tool Batch generator 执行区间通过 sys/threading profile 观察；不改 Handler。Probe 会引入开销，Smoke 的端到端与工具耗时只能用于可行性诊断，不能宣传为精确性能收益。Provider latency 包含消费流与 Eval 观察开销，不是服务端纯推理时间。

每个 response 的调用集合独立统计，不合并跨轮调用。只读独立机会暂按同一响应中不同 name+arguments 计算，是保守的候选机会指标；参数错误/拒绝/重复缓存与实际 Handler peak 必须分开查看，不能将 eligible 等同于 dispatched。ON/OFF 轨迹不同时，不归因 Scheduler。独立轨迹重放待 Smoke Gate 和语义审核后决定，不扩大真实模型运行次数。

Usage 缺失不以 estimator 补齐。任务失败、源码变化或 Usage 不完整时停止后续 Smoke，保留已有 Artifact。Provider 错误不归为模型推理失败。真实模型不返回多调用不判定 Scheduler 故障。
