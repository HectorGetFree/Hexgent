# C2 冻结指标口径

本轮无真实质量结果。固定 Fake 摘要包含标准事实，只验证协议与采集，不能证明真实压缩能力。

## 质量

人工逐条标注 PRESENT_CORRECT / MISSING / INCORRECT / UNCERTAIN，附摘要片段、来源引用和理由。非生成模型的独立审核者负责判定；实体、值、单位、范围和条件必须一致，允许释义。

- Critical Fact Recall = PRESENT_CORRECT 的 SUMMARY_REQUIRED 数 / 全部 SUMMARY_REQUIRED 数。UNCERTAIN 留在分母；另报将不确定项均视为正确的上界。
- Stale Fact Activation Rate = 被错误描述为当前有效的 OBSOLETE 数 / OBSOLETE 数。历史提及不算激活；不确定激活单列，不能填零。
- Unsupported Claim Rate = 无来源支持的事实断言数 / 摘要全部事实断言数。必须逐句完成 claim ledger；没有完整人工审查时为 NOT_AVAILABLE。纯格式句不计事实断言。
- EXTERNALLY_PROTECTED 只有在后续完整主请求中确认实际渠道提供事实才可排除；渠道缺失属于 Gate 失败，不能靠标签豁免。

## 请求与费用

pre/post_compaction_request_estimated_input_tokens 是 Production 事件中的 before_tokens/after_tokens：完整 instructions + input + tools 的输入估算，不含输出预留，不是历史 Token，不是 Provider Usage。

estimated_request_reduction = pre - post；ratio = reduction / pre。预算另外计入输出预留及 safety margin。

Provider 每条调用需单列 call_id、fixture/session/execution/span、调用类型、attempt、model、provider host、时间、状态、原始 Usage、缓存字段、input/output tokens。缺失保持 null/MISSING。Fake usage 为协议样本，不计实际成本。

费用 C = input_uncached × p_input + input_cached × p_cached + output × p_output（按计价单位换算）。失败请求没有 Usage 或计费规则未知时，完整费用为不可用，可观测小计不能冒充总量。

分别列 compaction 成本、此前已发生历史成本、后续 MAIN 成本、整个 run 累计成本。合成历史无已发生 Provider 账单，历史成本为 NOT_APPLICABLE，不是实际零成本运行。

break_even = ceil(compaction_cost / 单次后续请求净节省费用)，仅在分母正值且固定输入节省、输出、缓存和价格假设明确时成立。仅有估算节省时只能报告假设模型，不能宣称实测回本。

## 预注册结论

C2_QUALITY_AND_COST_SUPPORTED：全部样本可评价，必要事实均正确、无陈旧激活及无依据新增事实，全部调用 Usage/计费可核对，且在事先审批的后续请求序列或假设窗口内净费用为正收益。

C2_TRADEOFF_OBSERVED：可完整核对数据，但质量存在损失或成本不能回本，逐项报告，不设漂亮压缩率目标。

C2_INCONCLUSIVE：来源/请求归属无效、必要人工审查未完成、关键不确定事实或 Usage/价格缺失。质量严重错误另列阻断问题，不能被平均压缩率掩盖。
