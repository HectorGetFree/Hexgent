# Scheduler影响路径审计

主链：agent_loop → run_turn → call_model → execute_tool_batch → prepare/permission → durable Dispatch → scheduled execution → 原序持久化结果/Context → 同步ContextRecord提取 → Checkpoint → next model。

| 问题 | 判断 | 源码与事实 |
| --- | --- | --- |
| ON/OFF配置差异是否仅调度开关 | CONFIRMED（已记录配置） | 两侧artifact.execution_config仅tool_parallel.enabled不同；max_workers4/timeout60、其他预算权限相同。agent_turn.py:550传入parallel_limits；scheduler.py:259决定串并行。两次Provider首request相等。不能保证未观测外部环境相同。 |
| Tool Result交付顺序 | CONFIRMED相同算法/本次契约通过 | scheduler.py:598-700遍历scheduled_calls原序构造outputs；agent_turn.py:574-580原序追加；两侧delivery Oracle PASS；固定轨迹80次执行等价。 |
| 按完成序写Transcript | RULED_OUT Finished结果乱序；CONFIRMED完成事件可乱序 | scheduler.py:325-348按Future完成收取并发事件，:598后原序Finished。原Artifact完成事件可乱序，但不是function_call_output模型输入。 |
| Context Builder组织差异 | RULED_OUT配置直接改变排序 | context_manager.py:92物化active_messages；agent_turn.py:362-385请求，:574追加结果。实际内容不同来自不同模型输出/工具集合，不等于调度重排。 |
| Session持久化时序 | CONFIRMED实现路径不同但结果提交原序 | scheduler.py:437准备、权限；:537前后durable Dispatch；:598后主线程提交。并行期间实际完成时序不同；事件时间戳不直接作为工具结果注入。 |
| Artifact引用生成 | CONFIRMED动态路径，调度归因UNDETERMINED | session_store.py:408-459按正文hash生成文件、Session目录含时间；scheduler.py:632执行结果外置。glob builtin_tools.py:299返回绝对路径。原始模型可见不同root/session前缀来自Eval隔离，不能归因Scheduler。 |
| 共享可变状态/竞态 | PLAUSIBLE MECHANISM，未观察到本次内容错配 | tool_context.py:74-124共享cache带Lock；scheduler主线程提交、持久化与清cache。并发cache执行是潜在影响路径；本次共同6对和同轨迹重放未出现不等价，不证明所有竞态不存在。 |
| 模型可见完成时间/顺序 | RULED_OUT直接注入；CONFIRMED动态路径和Provider元数据保留 | agent_turn.py:362-385/492/574，Context保存response输出及工具结果，没有将peak/duration/completed UI events传入；Provider元数据、绝对路径、Session引用仍可见，不能全称为静态上下文。 |
| 超时/重试/取消 | CONFIRMED实现差异，本批未发生工具超时 | scheduler.py:259-269串行直接执行，:294-379并行轮询timeout/cancel，不能强杀线程。tool_retry.py按配置和副作用安全性判断。实际所有61调用success、attempt_count1，差异未触发。 |

Provider和辅助路径：agent_loop.py:69-110在完整Turn后同步执行extract_context_records；extract_context_records.py:47-70设置Timer8秒和scoped client retries0；call_model.py:109-170注册close回调、finally关闭流。取消记录不等于主线程即刻返回。close/读取退出的详细耗时 UNDETERMINED，不以推测解释全部残差。

Artifact证据：两侧artifact.json execution_config/events/oracles；provider-calls.json完整request.input；Transcript全部Dispatch/Finished/context_messages_appended；MATCHED_TOOL_RESULTS.json逐调用sequence；原cross-trace报告及逐Session Transcript。结论未仅依据单元测试。
