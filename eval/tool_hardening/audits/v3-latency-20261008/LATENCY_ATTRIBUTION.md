# 端到端耗时归因

## 可观测账目

| 项目 | ON 秒 | OFF 秒 | OFF − ON 秒 |
| --- | --- | --- | --- |
| Agent 总耗时 | 133.384043 | 550.134136 | 416.750093 |
| 主 Provider 观测耗时累计 | 119.015949 | 520.571539 | 401.555590 |
| 辅助 Provider 观测耗时 | 8.004048 | 8.004162 | 0.000114 |
| 工具 Pipeline profiler 执行段累计 | 0.285257 | 1.000136 | 0.714879 |
| 账面剩余，非独立实测分项 | 6.078789 | 20.558299 | 14.479510 |

主 Provider 差额占总差额 **96.35%**。这是观测贡献，不是 Scheduler 的因果收益。Provider wall 包含 SDK、网络、生成及部分 Eval 观测开销，不能进一步称为模型计算、排队或限流。

不把该表当成覆盖完整、互斥精确的 span 分解：残差仅为算术结果，Context Processing、Persistence、Handler 独立时间均 UNOBSERVED。工具 profiler 测的是 execute_tool_batch generator 各 resume 执行段（含观测干扰），不是独立 Handler I/O 时长，也不等同原始批次 start/end wall。不能直接与无 profiler 的 replay 数值相减。

UTC Provider 窗口 union：ON 127.020652 秒，OFF 528.516788 秒；窗口互不重叠。Agent 总量减 union 分别约 6.363391 / 21.617348 秒，但这些不是完整 Runtime 分类。ON/OFF Provider 的 perf_counter 累计与 UTC 差值并不完全相同，尤其 OFF 首请求 UTC span 26.484335 秒、perf_counter 26.542150 秒；不能混用时钟配平。时间戳微秒显示不代表微秒准确度。

## 额外请求与调用

按主轮序号，OFF 超过 ON 10 轮之后的主轮 11–14：

| OFF 主轮 | 全局 observed turn_id | 耗时秒 | input/output tokens |
| --- | --- | --- | --- |
| 11 | 12 | 21.529654 | 24680 / 85 |
| 12 | 13 | 25.090047 | 26358 / 89 |
| 13 | 14 | 20.529974 | 27901 / 116 |
| 14 | 15 | 126.654453 | 29609 / 2704 |

合计 193.804128 秒；前十个请求按序号累计差额 207.751462 秒。但 OFF 第14轮是终态，ON 第10轮也有终态，因此不能将上表视为四个语义上纯新增请求。按终态边界：最终输出 OFF 126.654453 / ON 42.991062 秒，差 83.663391 秒；其余请求累计差约 317.892199 秒，仍不是相同工作轨迹。

额外 23 次工具调用是总量差，不是可一一映射的新增 Calls。按主轮序号数量差：0,-2,1,5,4,4,4,3,0,1,1,1,1,0；总和23。完整名字/参数和 response_id 见 ROUND_ALIGNMENT.json，以及 OFF_TIMELINE.json summary.main_requests_detail。不得把跨轮累计调用当成单轮并发。

OFF 最慢最终请求占主 Provider 累计约24.33%，前三慢合计41.58%；长耗时并非只集中在一轮。相同首轮输入已出现约19秒 Provider差异，早于任何调度。

## Token / Context / Timeout

主请求 Usage 完整：ON input 97205 / output 3273；OFF input 228391 / output 5737。每轮 token 见 Timeline。最终 input ON16431 / OFF29609，说明实际请求上下文较长；累计input是重复消费计数，不是唯一文本数量。不能仅凭token差异建立耗时线性因果关系。辅助请求各取消一次，Usage 不可用，完整Run token成本不可重建。

两侧零 compaction 事件；ContextRecord 都在第4主轮后同步提取一次并失败，未注入新记录，后续提取被抑制。

- ON extraction durable seq99→100：21:50:10.092423→21:50:19.517124，北京时间，9.424701秒。
- OFF seq120→121：21:56:34.084362→21:56:49.989167，15.904805秒。
- Provider辅助窗口各约8.004秒。其外的准备/关闭/观测/等待没有细分证据，不能叫“第二次8秒超时”。区间嵌套在Agent时段内，不再次相加。

所有主请求 SUCCEEDED，工具均一次成功；没有记录到主 Runtime 重试或工具backoff。SDK max_retries=2 是配置，不是重试证据；HTTP attempt、Provider队列/限流/内部重试 UNDETERMINED。辅助with_options(max_retries=0, timeout=8)及Timer中断见 extract_context_records.py:47-70。

来源：两侧 artifact.json total_agent_latency_ms/total_tool_latency_ms；provider-calls.json 每请求 start/end/provider_latency_ms/usage；Timeline包含全部原始路径、数组索引及durable sequence。计时实现：eval/tool_hardening/v3_smoke.py:105-139；v3_provider.py:124-130,156-180。
