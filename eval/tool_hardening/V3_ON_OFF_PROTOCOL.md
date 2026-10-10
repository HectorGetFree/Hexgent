# V3 ON/OFF 与真实轨迹重放协议

本轮使用 `v3_experiment.py`，不修改 Production Runtime。独立新目录存放所有样本。

1. 通过当前 Provider Preflight；Tool Pipeline ON/OFF 各一次 Smoke，不进入正式样本统计。
2. Smoke 两侧工具执行/交付契约通过后，三个主题各执行三对；顺序为 ON→OFF、OFF→ON、ON→OFF。完整任务 prompt、源码哈希、模型/Provider、默认工具权限和正式配置被冻结，唯一配置差异为 `tool_parallel.enabled`。每次独立子进程与新 Session；隔离目录路径不同，不将端到端差异视为纯 Scheduler 因果证据。
3. 每次 Run 留存原始请求/响应、Transcript、逐轮调用分布和 metrics。逐字引用 Oracle 不放宽；语义未审核一律 `SEMANTIC_REVIEW_PENDING`，`task_oracle_pass=null`。
4. 新的执行契约 Oracle 区分 controlled tool error 与协议错误：错误路径、参数拒绝等正常失败输出保留操作状态，不伪称业务成功；未授权 dispatch、Call/Result 错配、缺少 durable fact 等才阻断 Pipeline。原 Hardened Smoke 的原始报告不重新定义或改写。
5. Usage 完整性分别统计 Main/Auxiliary/All。辅助 extraction 的既有 deadline 取消保留未知成本；只有所有 Usage 完整时才给总 Token，否则完整总量为 null，已知小计单列。SDK 内部 HTTP retry 不可观测，不能记为零。
6. 每主题选择按实验顺序第一条包含并行机会且契约有效的完整轨迹，不按性能或引用分数挑选。保留所有响应边界（包括零工具响应）、callId、原始 arguments 字符串和源快照；不重调用模型，不改写绝对路径。
7. 重放从原工作区只读，独立 Session 保存重放 durable 状态，不改变源 Artifact。重放复现已记录的 compaction 缓存失效边界；预算外置由同一 Production Pipeline 处理。外置结果解析完整正文再比较。源读取如依赖原 Run 中不断变化的状态而无法无损复现，应停止，不能伪造结果补救。
8. 每条完整轨迹预热一对，正式计时二十对，交替串并行顺序。每批独立计时，轨迹时间是原始顺序批次的 Pipeline 时间和，不包括初始化、Oracle、源码哈希及模型耗时。实际 Handler 并发使用独立非计时 profile；正式样本只记录 Scheduler 真实批次事件，不以配置上限冒充实测峰值。
9. P95 用 nearest-rank，Agent 每侧仅三样本时标为小样本诊断；不宣称稳定尾延迟。加速比按每条完整轨迹串/并耗时计算，不以批次平均代替；负收益样本原样保留。
10. Provider/代理异常、协议失败、配置不生效、源码变化或无损重放失败立即停止后续批次，报告保留已完成与中途样本。模型引用失败独立记录，不触发 Pipeline failure。

入口示例：

```sh
.venv/bin/python -m eval.tool_hardening.v3_experiment \
  --preflight eval/tool_hardening/runs/当前已通过的preflight.json \
  --output-dir eval/tool_hardening/runs/新的独立运行目录
```

旧 V1/V2/Hardened Smoke 不覆盖、不混合求平均。没有 commit/tag。
