# 首次分叉审计

## 原始证据

- ON：`/Users/hector/dev/hexgent/eval/tool_hardening/runs/v3-on-off-replay-20261008-r1/smoke/tool_pipeline/1/ON/provider-calls.json`
- OFF：`/Users/hector/dev/hexgent/eval/tool_hardening/runs/v3-manual-20261008T135408Z/tool_pipeline/pair-1/OFF/provider-calls.json`
- 完整逐轮身份、原始 Calls、输入 canonical hash、逐字段原始/规范化比较：`ROUND_ALIGNMENT.json`。
- 精确匹配工具参数的正文、状态、权限比较及 durable sequence：`MATCHED_TOOL_RESULTS.json`。

## B1：首次输入分叉

主轮 1 的已保存 request 对象完全相等：model、instructions、input、tools、max_output_tokens、stream 均相同。input 只有一条 user message；没有独立 Developer message、Memory/ContextRecord 注入。模型为 gpt-6.1-sol，Provider 相同，SDK options 相同。这里只能确认 captured request；HTTP wire bytes/headers、Provider 隐藏默认参数和路由没有保存，不宣称真实网络字节完全一致。

主轮 2 的 `input` 首次不同，规范化 workspace/session 前缀后仍然不同；instructions、工具定义和其余请求参数相同。实际 HTTP 输入已包含上一轮不同的 assistant/function_call 和工具结果。

## B2 / B3：首次输出与工具轨迹分叉

第一轮输出就在 Scheduler 执行前分叉。两侧 `/0/tool_calls`：

| 顺序 | ON glob pattern | OFF glob pattern |
| --- | --- | --- |
| 1 | hexgent/tool/**/* | hexgent/tool/** |
| 2 | hexgent/loop/agent_turn.py | hexgent/loop/agent_turn.py |
| 3 | **/test* | tests/** |

数量均 3，同一响应边界，但第 1/3 个参数不同，所有 Call ID 不同。OFF `/0/response/output` 还有 reasoning 条目，ON 没有；不能将它当成动态字段删除以制造等价。原始响应分别为 `resp_ft_fd434ce4a66f49bcc4a4466a071eda6f` / `resp_ft_b5a45b5ef05e78d110f91dbfc2ed1789`。

首轮 Provider 开始：ON 13:49:32.707663 UTC，OFF 13:54:14.201631 UTC（均2026-10-08）；首轮就分别约 7.550 / 26.542 秒。第一次工具 Dispatch 在模型响应完成之后，不能解释此前已存在的输出和延迟差异。

## B4：可对应工具结果

找到 6 对相同工具名、规范化参数的调用：共同 glob 与 5 个源码完整读取。5 对 read_file 正文逐字一致；glob 原文因绝对 workspace 前缀不同而不一致，规范化后相同。6 对状态、权限裁定均相同；正文使用 Finished 指向的完整 Artifact，不仅比较预览。

共同 glob 的 durable Finished 为两侧 sequence 21。5 对源码读取 ON sequence 48/50/51/53/54，OFF 71/73/74/76/77。原始路径、Call ID 和返回正文不被改写。其余调用参数不同，不强行配成相同调用；这是跨轨迹证据，不替代同轨迹重放的 100% 等价结果。

绝对 workspace 路径和 Session Artifact 引用实际会进入模型输入，来自 Eval clone/session，而不是并行开关；规范化相同不等于原始模型输入相同。

## B5：逐轮传播

| 主轮序号 | ON calls | OFF calls | 请求原始相等 | 工具语义序列相等 |
| --- | --- | --- | --- | --- |
| 1 | 3 | 3 | 是 | 否 |
| 2 | 5 | 3 | 否 | 否 |
| 3 | 5 | 6 | 否 | 否 |
| 4 | 1 | 6 | 否 | 否 |
| 5 | 1 | 5 | 否 | 否 |
| 6 | 1 | 5 | 否 | 否 |
| 7 | 1 | 5 | 否 | 否 |
| 8 | 1 | 4 | 否 | 否 |
| 9 | 1 | 1 | 否 | 否 |
| 10 | 0 | 1 | 否 | 否 |
| 11–14 | 无 | 各1,1,1,0 | 无法对齐 | 无法对齐 |

主轮排除辅助请求；不能将观测 turn_id 5 当成主轮 5。分叉后序号对齐不代表同一规划状态。

已确认：不同首次工具选择 → 不同结果集合 → 下一请求 input 不同 → 后续实际轨迹、输入长度和请求数量不同。没有证明每条后续选择由哪条前序差异导致，更没有证明这些差异由 Scheduler 引起。未指定 seed/temperature；相同显式参数不保证确定性，Provider 随机性/路由/负载具体原因 UNDETERMINED。

源码：`hexgent/loop/agent_turn.py:362-385` 请求构造及模型调用，`:492` response 输出追加，`:550-580` 工具执行与 Context 追加。首次选工具在该执行路径之前发生。
