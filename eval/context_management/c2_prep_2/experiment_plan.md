# C2实验计划修订：15样本，未获付费执行批准

C2-A：原12条短历史受控事实Smoke，单独报告；保留原阈值控制，不宣称默认长历史。

C2-B：新3条SYNTHETIC_LONG_HISTORY，默认ContextLimits：128000 window、64000 proactive、24000 recent、2000 summary output、2 attempts、1 partial。完整参数见每个frozen-state.json。源码正文是实际冻结快照，但模型轨迹、台账和历史调用为合成，不能推广为真实生产负载。

## 执行合同与上界

原则上每样本1次摘要；只对COMPACTION连接真实SDK，下一次MAIN使用单个Fake终态定位请求，不产生工具调用或prompt-too-long重试。不能把这个混合执行描述成真实Agent端到端行为实验。

在该明确合同下，最多1次partial后再1次operation，每operation最多2attempts，15×2×2=60次真实摘要create。必须SDK Retry=0。MAIN最多15次Fake，不计实际Provider费用；全观测调用保守总上界75。若允许新的MAIN轮次、反应式压缩或持续历史增长，60不再适用，必须重新审批。

预计真实摘要15次；最坏60次，不再沿用旧48。输出参数上界60×2000=120000 tokens，但实际可计费语义仍待确认。最大输入=60×I_max，I_max未知，金额上界=60×(I_max×P_input_max+2000×P_output_max)/计价单位，并计缓存写入/路由倍率/失败请求风险；不能填入未经确认的金额。

共享硬Budget在每次调用前预留最大额度。余额不足、未知输入上界、价格未确认、Usage缺失、实际Usage越界、归属异常、源码/Fixture hash变化立即停止；失败计费按预留上界保持占用。普通质量失败保留并继续，不能替换样本或重跑至PASS。

## 新发现的样本Gate

L2:fact:7已冻结为SUMMARY_REQUIRED，但实际压缩切分将它留在recent tail，而不是摘要输入。不能要求摘要生成它，否则会把可靠历史渠道保留误算成摘要召回。未改写Fact ID或分类；reviewed_dry_run.json保存该冲突。L2正式质量计分BLOCKED，等待人工批准单独的Oracle责任渠道修订方案；不能直接按5条全计摘要分母。

L1/L3大量使用同一源码快照，题材不同但底层正文覆盖重叠；L3含合成进度与失败恢复状态，尚未形成多阶段失败/恢复工具轨迹。长历史触发Gate通过，代表性Gate PARTIAL，不能宣传三类独立真实负载。进一步样本设计需要审阅，不能修改既有冻结样本制造通过。

人工审批前需：解决L2责任渠道冲突、审核长历史代表性、补齐价格/Token硬上界/失败计费、核对全部哈希与请求审计。当前BLOCKED_FOR_C2，不运行C2/C3。
