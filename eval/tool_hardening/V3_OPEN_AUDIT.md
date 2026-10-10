# V3 自然审计任务多对 ON/OFF

新实验入口：`eval.tool_hardening.v3_open_audit`。不会覆盖历史V3。

唯一用户query：`请帮我审计一下 \`hexgent/tool\` 模块的设计`。

不附加指定问题、必须读取的文件、固定调用序列、JSON输出或逐字引用要求；不提示并发。任务结果人工审核，`strict_citation_pass=null`、`strict_citation_status=NOT_APPLICABLE`，不得因自由文本输出判旧JSON Oracle FAIL，也不得据Agent completed宣布任务PASS。旧V3的Prompt/Oracle默认行为保留。

执行边界继续保持：真实Production Agent Loop/Tool Pipeline/read_file/glob、默认权限、确认请求deny、隔离源码workspace、16轮/100calls/600秒预算。无任意shell、写工具或新工具。放宽自然语言任务契约不等于放宽执行权限。

默认5对（10个Agent Run），第1对ON/OFF、第2对OFF/ON交替。每次新子进程、新workspace和Session。只改变parallel.enabled；一对结束验证其他config相同。没有自动Replay或重复失败样本。Provider失败/协议错误/源码变化停止并保留日志，不过滤负面耗时。

每个Run保留：artifact.json、provider-calls.json、metrics.json、timing-events.json、evidence-index.json及完整workspace/.hexgent Session/Transcript/Checkpoint/tool-results。父report.json保留各样本和逐对第一请求相等性、工具语义轨迹相等性；manifest冻结源码、Eval实现、query、模型、Provider和preflight引用。不保存API Key。

timing-events使用同一perf_counter起点记录Handler call/return、Pipeline resume/suspend和Runtime事件，可关联Call ID及Provider turn。两侧都开启相同Profiler，因此性能包含诊断开销；不得称无观测开销性能。Provider完整request/response、UTC起止/monotonic duration/Usage继续由现有Observer记录。SDK内部attempt、stream-close细分或Context/Persistence独立span仍不可观测，不能填0或猜测。

report区分所有已观察样本（包括失败）和有效配对统计；缺Usage保留null。P95 nearest-rank在5个样本下等于最大值，仅作小样本诊断；多次运行不能消除Provider非平稳性或模型规划差异，更不能直接证明Scheduler的端到端因果收益。

## 执行

先运行现有provider_preflight显式请求，确保PASS。将其JSON路径传给新入口：

```sh
.venv/bin/python -m eval.tool_hardening.v3_open_audit \
  --preflight "$V3_PREFLIGHT" --output-dir "$V3_OUTPUT" --pairs 5
```

断开终端后可对同一目录加`--resume`，必须使用相同preflight、pairs、源码、Eval和Provider配置。完整样本依据evidence-index哈希校验后跳过，不重新调用模型。若样本已有目录但metrics或index不完整，停止要求人工检查，绝不静默覆盖/自动重跑。已记录stop_reasons的失败样本也不会自动重试。

本次交付仅修改脚本，尚未启动实验。可选离线回归命令：`.venv/bin/python -m unittest eval.tool_hardening.test_v3_open_audit eval.tool_hardening.test_v3_experiment eval.tool_hardening.test_v3_provider -q`。

## Provider失败后的显式重试

`--resume --retry-failed`仅为已完整保存的Provider失败创建`ON-attempt-2`/`OFF-attempt-2`等独立目录，每次命令每个失败侧至多追加一次；已成功侧不重跑。协议/源码错误或缺少完整证据的目录仍停止。不自动循环重试。原始失败的Artifact/metrics/index/log保持不变。

升级Eval后旧实验另加`--accept-eval-revision`：校验Production源码和Provider不变，将新的Eval哈希、变更文件及前版manifest哈希写入独立`manifest-revision-0001.json`，旧manifest不改。报告写入`report-manifest-revision-0001.json`，不覆盖旧report。后续续跑自动选最新修订。每个新attempt记录其实际Eval manifest引用和哈希，不能声称所有attempt使用同一Eval revision。

原始配对统计不纳入重试配对；`retry_pairs`/`retry_attempts`另列全部历史和最新结果。`all_observed`统计使用各侧原始attempt，失败不被成功重试替换。恢复后的配对仍保留不同执行时刻/修订的混杂因素，不直接归因为调度收益。

## 固定总次数（包括失败）

使用`--count-failures --pairs 5`：ON/OFF各总共5次，成功失败都消耗次数；旧attempt也计入预算。已记录Provider失败或Agent未完成后继续下一侧/下一次，不为凑5次成功无限重试。新样本保存到counted-attempts；报告为counted-report-<manifest名称>.json，保留每次失败、全部耗时和Usage。不使用旧的retry-pair主指标。已有2次ON失败时仅再执行3次ON及5次OFF。

该模式不与`--retry-failed`混用。未完整落盘、证据损坏、源码/Eval/Provider变化或工具协议异常属于安全/基础设施阻塞，仍停止人工处理。完成状态BUDGET_COMPLETED只证明次数达到，不代表任务或Provider成功。
