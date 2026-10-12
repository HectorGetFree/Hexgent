"""人工核对后的Stage2判断固化；只定位原始摘要输入，不运行模型。"""
import argparse
import ast
import json
from pathlib import Path
from eval.context_management.c1_independent import save
from eval.context_management.c2_prep_3.prepare import read_json


FACTS={
 'F2-01':{1:'当前窗口为每周二、周五09:30',2:'单笔超过7300元须双人复核',3:'RF-28缺负责人签名',4:'支付未获授权'},
 'F3-02':{1:'清单比对已完成',2:'权限审查已完成，确认当前只读角色范围',3:'guide.md 的 L42、L88 两处引用仍待作者确认',4:'当前未发布',5:'取得作者对两处引用的确认并完成引用验证，再提交最终检查单'},
 'F4-03':{1:'调用标识为 CALL-BIRCH-52',2:'崩溃发生在提交后、Finished 未持久化',3:'Handler 进入 1 次',4:'副作用提交 1 次',5:'恢复期间未再次派发',6:'当前处置为需人工核验'},
 'L1':{1:'监听端口 19347',3:'快照 CONFIG-ELM-2026-R8',4:'批处理失败仅重试未提交项',5:'提交前核对 Lease 归属与输入修订号',7:'最终决策为完成只读审查，发布仍待批准'},
}

# 每条为人工判定的独立实质性断言；source_anchor只用于定位审过的来源，不自动判正确。
SHORT={
'F2-01': '''
核对 F2-01:source:0|核对资料来源 F2-01:source:0|SUPPORTED_PARAPHRASE|用户阅读目标与可见历史一致，不表示真实执行退款任务。
输出当前窗口|输出当前窗口|SUPPORTED_PARAPHRASE|用户明确要求输出当前窗口。
复核门槛|复核门槛|SUPPORTED_EXACT|用户输出要求包括复核门槛。
待补材料|待补材料|SUPPORTED_EXACT|用户输出要求包括待补材料。
两项已替代旧规则|已经被替代的两项旧规则|SUPPORTED_PARAPHRASE|旧结算与单人复核分别被替代。
原始退款账目只读|禁止改写原始退款账目|SUPPORTED_PARAPHRASE|原输入明确保护原始账目。
审批凭证关联退款批号|审批凭证应关联退款批号|SUPPORTED_PARAPHRASE|保持关联对象，未转换为客户豁免。
不得建立客户永久豁免|不是对客户建立永久豁免|SUPPORTED_PARAPHRASE|限定审批凭证用途。
跨币种单列|跨币种记录需要单独列项|SUPPORTED_PARAPHRASE|正确保留跨币种独立处理要求。
不视为金额不足|不能把币种差异当成金额不足|SUPPORTED_PARAPHRASE|不以币种差异推断金额不足。
已读取 source-0.md|source-0.md|SUPPORTED_INFERENCE|call及对应output在合成历史中存在，不是本轮真实read_file证据。
收到负责人明确生效更正|负责人明确更正|SUPPORTED_PARAPHRASE|来源为用户明确更正，不是工具自动冲突处理。
最终检查结论|最终资料检查|SUPPORTED_PARAPHRASE|后续用户检查资料作当前依据。
当前窗口为每周二、周五09:30|结算窗口=每周二及周五09:30|SUPPORTED_PARAPHRASE|周二/周五与时刻一致，保持当前状态。
单笔超过7300元须双人复核|复核门槛=单笔超过7300元须双人复核|SUPPORTED_EXACT|保留单笔/超过而非大于等于条件。
RF-28缺负责人签名|当前缺项=批次RF-28的负责人签名|SUPPORTED_PARAPHRASE|批次与缺失材料一致。
列表行数不等于待支付笔数|退款列表的行数与待支付笔数不同|SUPPORTED_PARAPHRASE|正确区分两种统计对象。
含撤回、已支付记录|有撤回记录和已支付记录|SUPPORTED_PARAPHRASE|两类历史行原输入存在。
支付未获授权|支付状态=未获授权|SUPPORTED_PARAPHRASE|未将资料完成误写为获授权。
资料准备完成不等于审批齐全|准备工作完成不代表支付授权齐全|SUPPORTED_PARAPHRASE|条件/逻辑区分，不宣称支付准备已完成。
复核条件未满足不得进入支付准备|未达到复核条件的批次可完成资料核对，不能进入支付准备|SUPPORTED_PARAPHRASE|保留门槛和动作限制。
明确废止每日23:00结算|旧结算窗口=每日23:00|SUPPORTED_PARAPHRASE|源明确废止，摘要不是激活旧规则。
仅单人复核两项旧规则|旧复核规则=仅需单人复核|SUPPORTED_PARAPHRASE|同属明确废止记录。
旧规则仅作历史依据|保留旧方案的历史依据|SUPPORTED_PARAPHRASE|不复活旧方案。
不得由失败重试日志|日志不能改写财务规则|SUPPORTED_PARAPHRASE|失败日志不改变当前财务规则。
过去成功批次推断规则放宽|不要根据过去的一次成功批次推断当前规则放宽|SUPPORTED_PARAPHRASE|保持来源限制。
避免重新启用旧决定|最终建议不能把旧方案复活|SUPPORTED_PARAPHRASE|下一动作防止陈旧决定。
''',
'F3-02': '''
核对资料来源 F3-02|核对资料来源 F3-02:source:0|SUPPORTED_PARAPHRASE|合成历史核对目标一致。
source:0、source:1、source:2 均已读取核对|已核对|SUPPORTED_INFERENCE|三组read_file及output/确认都在原摘要输入；不是本轮真实工具审计。
仅操作合成工作区文档|任务仅操作合成工作区文档|SUPPORTED_EXACT|保留作用范围。
不连接远程发布系统|不连接远程发布系统|SUPPORTED_EXACT|不扩大到部署任务。
不得扩大资料访问权限|不得扩大资料访问权限|SUPPORTED_EXACT|原来源中也存在，不仅从Pinned取证。
清单比对已完成|清单比对状态=已完成|SUPPORTED_PARAPHRASE|完成对象仅清单比对。
权限审查已完成|权限审查状态=已完成且不扩大范围|SUPPORTED_PARAPHRASE|完成对象仅权限审查。
确认当前只读角色范围|权限审查已经确认只读角色范围|SUPPORTED_PARAPHRASE|不扩大角色访问范围。
文件齐全不等于引用正确|文件存在与引用正确是两种结论|SUPPORTED_PARAPHRASE|不混淆文件存在与引用校验。
被撤销的历史角色不计入当前可读范围|被撤销角色不能被计入当前可读范围|SUPPORTED_PARAPHRASE|保留失效边界。
旧“作者全部确认”状态已废止|旧作者确认状态=全部已确认|SUPPORTED_PARAPHRASE|原历史项明确废止，不错误激活。
作者实际仅确认第一批文档|只确认了第一批文档|SUPPORTED_PARAPHRASE|确认范围未扩大。
大部分引用已修正|引用校验修正了大部分链接|SUPPORTED_PARAPHRASE|未宣称所有引用完成。
guide.md 的 L42、L88 两处引用仍待作者确认|待确认引用=guide.md的L42与L88|SUPPORTED_PARAPHRASE|文件名/两处行号/待确认状态一致。
当前未发布|当前发布状态=未发布|SUPPORTED_PARAPHRASE|未把资料核对当作发布成功。
发布门槛要求关闭全部待确认链接|当前发布门槛要求所有待确认链接关闭|SUPPORTED_PARAPHRASE|保留全部关闭条件。
取得作者对两处引用的确认并完成引用验证|作者确认两处引用后提交检查单|SUPPORTED_PARAPHRASE|先确认/校验后提交，无跳步。
再提交最终检查单|再提交最终检查单|SUPPORTED_EXACT|未来动作未写成已完成。
证据目录可用于回读链接位置|证据目录记录了发现的链接位置，可用于回读|SUPPORTED_PARAPHRASE|来源指向与用途一致。
最终报告须区分本地检查完成与外部发布未执行|最后的报告需要区分本地检查完成和外部发布尚未执行|SUPPORTED_PARAPHRASE|不声称外部发布已发生。
原检查单错误标记作者确认完成|原检查单曾将作者确认标记为完成|SUPPORTED_PARAPHRASE|正确记录旧状态错误。
已纠正|该状态被纠正|SUPPORTED_EXACT|使用更正后状态。
不得沿用旧检查单宣布全部通过|不能按旧检查单宣布全部通过|SUPPORTED_PARAPHRASE|没有把错误状态当作最终结论。
不得跳过引用验证直接发布|不能跳过引用验证直接发布|SUPPORTED_EXACT|保留发布依赖。
''',
'F4-03': '''
核对资料来源 F4-03:source:0、source:1、source:2|已核对|SUPPORTED_INFERENCE|三条来源的read_file/回填/确认都在合成历史中，非真实故障注入实测。
仅基于合成 Fixture 资料|全部数据来自合成Fixture|SUPPORTED_PARAPHRASE|限定模拟资料，不外推业务账本。
不进行新的执行|不进行新的执行|SUPPORTED_EXACT|不执行重放工具。
区分工具协议事实与外部业务状态|区分工具协议事实与外部业务状态|SUPPORTED_EXACT|两种状态不能相互替代。
已读取并核对三份来源|已核对|SUPPORTED_INFERENCE|可见合成阅读历史，不声称本轮真实读取Handler。
调用标识为 CALL-BIRCH-52|调用标识=CALL-BIRCH-52|SUPPORTED_PARAPHRASE|call实体相同。
Handler 进入 1 次|Handler进入次数=1次|SUPPORTED_PARAPHRASE|没有把迟到诊断计作第二次调用。
副作用提交 1 次|副作用提交次数=1次|SUPPORTED_PARAPHRASE|提交次数不等于Exactly-once保证。
崩溃发生在提交后、Finished 未持久化|崩溃边界=副作用提交后且Finished未持久化|SUPPORTED_PARAPHRASE|严格保持先后边界。
恢复期间未再次派发|恢复派发=未再次派发|SUPPORTED_PARAPHRASE|未重试未知副作用。
迟到诊断/结果不能证明第二次调用|迟到诊断来自原调用，不是第二次调用的证据|SUPPORTED_PARAPHRASE|诊断仍属于原call。
也不能覆盖已提交状态|迟到结果未覆盖已提交状态|UNCERTAIN|来源只是该次未覆盖的历史观察，不能提升为一般不能覆盖保证；摘要有合成范围限定但措辞偏强。
该窗口无自动 Exactly-once 证据|没有证据证明崩溃窗口具备自动Exactly-once|SUPPORTED_PARAPHRASE|保留无法证明Exactly-once的限制。
副作用结果保守标记为未知|恢复路径保守标记未知结果|SUPPORTED_PARAPHRASE|不把未知结果改成成功或失败。
当前处置为需人工核验|当前处置=需要人工核验未知结果|SUPPORTED_PARAPHRASE|保留人工核验义务。
旧的“立即自动重试”建议已失效|该建议已失效|SUPPORTED_PARAPHRASE|旧建议只作历史，不激活。
明确人工核验的调用|指定需人工核验的调用|SUPPORTED_PARAPHRASE|交接对象与未来动作一致。
已知副作用|已知副作用|SUPPORTED_EXACT|交接应列已知副作用，不作未知成功推断。
恢复期间派发行为|恢复期间派发行为|SUPPORTED_EXACT|交接需要记载恢复派发。
不得仅凭文件存在|不能仅凭文件存在推断安全重试|SUPPORTED_PARAPHRASE|不把文件存在当提交完成证据。
工具结果缓存判断可安全重试|工具结果缓存不能替代外部账本|SUPPORTED_INFERENCE|仅工具缓存不能消除未知提交窗口。
需以外部账本和恢复记录为准|该事实来自恢复记录|SUPPORTED_INFERENCE|来源记录和外部账本共同约束核验，不是线程终止保证。
'''}


LONG1='''
继续逐文件只读审查 Hexgent|0|继续只读审查|SUPPORTED_PARAPHRASE|任务是源码阅读而非执行测试。
区分源码实现、测试断言与尚未验证行为|0|区分实现、测试断言与尚未验证的行为|SUPPORTED_PARAPHRASE|用户明确三层证据。
最近已读 hexgent/events.py|28|hexgent/events.py|SUPPORTED_INFERENCE|原摘要输入最后一个read_file及对应output是events.py。
只读，不修改或部署|0|继续只读审查|SUPPORTED_PARAPHRASE|只读任务与台账发布待批准限制；不能宣称已发布。
记录默认参数|0|记录参数默认值|SUPPORTED_EXACT|返回头要求记录参数默认值。
入口调用方|0|入口调用方|SUPPORTED_EXACT|返回头的阅读要求。
状态转换|0|状态转换|SUPPORTED_EXACT|返回头的阅读要求。
边界检查|0|边界检查|SUPPORTED_EXACT|返回头的阅读要求。
冻结源码仅为阅读证据|0|它是阅读证据，不是本轮执行测试结果|SUPPORTED_PARAPHRASE|不冒充测试执行结果。
不是执行测试结果|0|不是本轮执行测试结果|SUPPORTED_PARAPHRASE|保留来源限制。
不是演练部署配置权威|0|不是合成演练部署的配置权威|SUPPORTED_PARAPHRASE|源码默认值不覆盖合成台账。
已阅读包入口|0|hexgent/__init__.py|SUPPORTED_INFERENCE|存在对应冻结阅读返回，仅合成历史。
CLI 全部适配文件|4|hexgent/adapters/cli/event_renderer.py|UNCERTAIN|输入列出了多份CLI文件，但没有目录清单证明全部文件；仅按已读列表解释。
integration/routes.py|9|hexgent/adapters/integration/routes.py|SUPPORTED_INFERENCE|该返回在原摘要输入内。
Web 适配全部文件|12|hexgent/adapters/web/app.py|UNCERTAIN|同样缺少完整目录枚举，全部是过强范围量词。
agent_engine.py|20|hexgent/agent_engine.py|SUPPORTED_INFERENCE|对应正文在原输入。
client.py|21|hexgent/client.py|SUPPORTED_INFERENCE|对应正文在原输入。
config.py|22|hexgent/config.py|SUPPORTED_INFERENCE|对应正文在原输入。
context 包|25|hexgent/context/context_builder.py|SUPPORTED_INFERENCE|实际包括__init__/budget/builder/compaction/manager各正文，限定已读文件。
events.py|28|hexgent/events.py|SUPPORTED_INFERENCE|对应正文在原输入。
此前每次仅确认阅读|0|已阅读该工作项|SUPPORTED_INFERENCE|可见历史assistant都是固定阅读确认，不是详细报告。
尚未形成详细审查报告|0|已阅读该工作项|SUPPORTED_INFERENCE|完整可见历史只有阅读确认，无最终报告。
未读取测试|0|hexgent/__init__.py|SUPPORTED_INFERENCE|实际所有read路径为hexgent源码，没有tests路径；限可见历史。
未执行测试|0|不是本轮执行测试结果|SUPPORTED_PARAPHRASE|头部以及只读call集合无测试执行。
监听端口 19347|0|当前演练配置监听端口=19347|SUPPORTED_PARAPHRASE|台账实体、值和当前状态一致。
快照 CONFIG-ELM-2026-R8|10|当前快照编号=CONFIG-ELM-2026-R8|SUPPORTED_PARAPHRASE|快照ID严格相同。
批处理失败仅重试未提交项|20|批处理失败后仅重试未提交项|SUPPORTED_PARAPHRASE|策略与作用范围未扩大。
提交前核对 Lease 归属与输入修订号|24|提交前需要核对 Lease 归属与输入修订号|SUPPORTED_PARAPHRASE|保留提交前两项核对条件。
最终决策为完成只读审查，发布仍待批准|26|本轮最终决策=完成只读审查，发布仍待批准|SUPPORTED_PARAPHRASE|审查与发布分离。
旧端口 19000|1|早期演练端口=19000|SUPPORTED_PARAPHRASE|原台账明确废止，摘要不作当前端口。
“全部项无条件重试”已废止|13|旧重试策略=全部项无条件重试|SUPPORTED_PARAPHRASE|旧策略明确废止。
Web 源码默认 127.0.0.1:8000 不替代台账|11|@_parse_arguments|SUPPORTED_PARAPHRASE|__main__默认地址127.0.0.1/端口8000；头部明确不是演练配置权威。
首次消费生成器时占用单 Run 锁|20|@run|SUPPORTED_PARAPHRASE|run生成器首次进入时run_lock检查并置_run_active。
submit 取代待续快照|20|@_build_run_state|SUPPORTED_PARAPHRASE|submit显式记录superseded并清_pending_run_state。
continue 禁止携带 query|20|@_normalize_request|SUPPORTED_PARAPHRASE|显式continue请求query不为None则ValueError。
且要求快照|20|@_build_run_state|SUPPORTED_PARAPHRASE|无pending快照拒绝continue。
未取消|20|@_build_run_state|SUPPORTED_PARAPHRASE|cancellation_committed拒绝continue。
仅 interrupted/incomplete 保留续跑快照|20|@run|SUPPORTED_PARAPHRASE|Stop终态集合判断仅这两种，其他清缓存。
字符串请求规范化路径直接返回|20|@_normalize_request|SUPPORTED_EXACT|isinstance(str)直接RunRequest.submit。
非空校验与显式 RunRequest 路径不同|20|@_normalize_request|SUPPORTED_INFERENCE|字符串分支提前返回，而显式对象分支后面才检查非空；静态差异不等于已复现故障。
固定 Memory Scope|20|self.memory_store|SUPPORTED_PARAPHRASE|Engine绑定root及同一个memory_store，不扩展多租户隔离。
约束管理|20|@manage_pinned_constraint|SUPPORTED_PARAPHRASE|活跃Run拒绝管理API。
上下文记录更新拒绝活跃 Run|20|@upsert_context_records|SUPPORTED_PARAPHRASE|同run_lock检查_run_active。
每 Session 单活跃 Run|16|@start_run|SUPPORTED_PARAPHRASE|start_run/continue_run检查active handle，不声称分布式锁。
同步 Core 在线程消费|16|asyncio.to_thread(self._consume_run|SUPPORTED_PARAPHRASE|_drive_run委托线程消费同步Core。
interrupted 映射 paused|16|@_map_terminal_status|SUPPORTED_PARAPHRASE|网络终态映射为paused。
审批默认等待 300 秒|16|PERMISSION_WAIT_TIMEOUT_SECONDS = 300.0|SUPPORTED_EXACT|常量及wait使用该常量。
首次响应|16|@resolve|SUPPORTED_PARAPHRASE|PendingPermission.resolve首次设置并唤醒。
相同响应重放接受|16|@resolve|SUPPORTED_PARAPHRASE|已resolved时仅response_id和decision相同返回True。
事件缓存为内存|16|events: list|SUPPORTED_PARAPHRASE|RunHandle列表缓存events；不推断持久化SSE。
序号从 1 递增|16|sequence=len(handle.events) + 1|SUPPORTED_EXACT|append构造序号为现有长度+1。
公开 Core 流仅 text_delta/tool_started/tool_finished/plan_updated|14|STREAM_EVENT_TYPES = frozenset|SUPPORTED_PARAPHRASE|集合只有四种，serialize过滤不在集合的事件。
RunFinished 更新终态|16|if isinstance(event, RunFinishedEvent)|SUPPORTED_PARAPHRASE|append映射终态和结束时间。
但不公开|14|@serialize_agent_event|SUPPORTED_PARAPHRASE|RunFinished映射为run_finished而不在公开集合。
Adapter 可额外发布审批|16|permission_requested|SUPPORTED_PARAPHRASE|Adapter另建审批事件，不受Core四项限定。
失败事件|16|@_mark_failed|SUPPORTED_PARAPHRASE|_mark_failed独立Adapter事件。
Integration 路由均要求环境 token 精确 Bearer 匹配|9|@_require_integration_token|SUPPORTED_PARAPHRASE|各路由调用该函数；缺token或精确比较不匹配401。
SSE 非权威|9|非权威 Integration SSE|SUPPORTED_EXACT|源码声明/接口只转发内存事件，非持久化事实来源。
从零游标开始|9|cursor = 0|SUPPORTED_EXACT|generate初始cursor=0。
无重连头处理|9|@stream_events|SUPPORTED_INFERENCE|完整该函数无Last-Event-ID读取。
普通 /api 路由本文件未见鉴权|15|router = APIRouter|SUPPORTED_INFERENCE|完整routes.py函数不检查Auth；严格限本文件，未推断应用全无鉴权。
负 Last-Event-ID 经 max(非负查询游标,负值)|15|cursor = max(cursor, int(last_event_id))|SUPPORTED_INFERENCE|Query ge=0，max不能小于0。
不会被后续负数检查拒绝|15|if cursor < 0:|SUPPORTED_INFERENCE|检查对象是max后的cursor而非原header负值；静态疑点，未真实请求复现。
拒绝未知段/字段|22|@load_config|SUPPORTED_PARAPHRASE|unknown_sections检查，_load_section检查unknown_fields。
严格 bool/int|22|@_validate_field|SUPPORTED_PARAPHRASE|bool和int分别type精确校验，避免bool作为int。
正数|22|@_validate_field|SUPPORTED_PARAPHRASE|数值须>0，部分例外字段有专门处理；摘要写部分关联规则。
部分关联约束|22|@_validate_relationships|SUPPORTED_PARAPHRASE|关联容量、输入、recent等规则在此，不声称全部约束穷尽。
默认 TOML 不存在直接返回默认配置|22|@load_config|SUPPORTED_PARAPHRASE|config_path None且不存在则HexgentConfig()。
客户端 load_dotenv(override=True)|21|load_dotenv(override=True)|SUPPORTED_EXACT|两个函数同样override=True。
WebSettings override=False|19|load_dotenv(override=False)|SUPPORTED_EXACT|Web环境设置不会覆盖显式环境。
窗口128000|24|context_window_tokens: int = 128_000|SUPPORTED_EXACT|源码默认值，不代表Provider容量。
输出8000|24|reserved_output_tokens: int = 8_000|SUPPORTED_EXACT|配置输出预留而非Usage。
安全8000|24|safety_margin_tokens: int = 8_000|SUPPORTED_EXACT|配置安全余量。
主动64000|24|proactive_threshold_tokens: int = 64_000|SUPPORTED_EXACT|默认阈值，不与短样本实际配置混淆。
注入12000|24|runtime_injection_tokens: int = 12_000|SUPPORTED_EXACT|默认固定运行时注入预算。
约束4000|24|pinned_constraints_tokens: int = 4_000|SUPPORTED_EXACT|保护区上限。
摘要2000|24|summary_output_tokens: int = 2_000|SUPPORTED_EXACT|摘要输出额度。
近期24000|24|recent_context_tokens: int = 24_000|SUPPORTED_EXACT|默认近期保留。
应急8000|24|reactive_recent_context_tokens: int = 8_000|SUPPORTED_EXACT|应急近期保留。
主动阈值 min(窗口-输出-安全,主动值)|24|@compaction_threshold_tokens|SUPPORTED_PARAPHRASE|物理值先max(1,...)再min；128K默认数值下max保护无影响。
硬阈值窗口-输出|24|@blocking_threshold_tokens|SUPPORTED_PARAPHRASE|同样有max(1,...)；摘要略下界保护，不改变默认算值。
保留固定区块不截断|25|@build_context_details|SUPPORTED_PARAPHRASE|系统、Pinned、Plan、Skill整块拼接，固定区超预算告警/拒绝，只有召回逐条预算。
约束超预算拒绝|25|PINNED_CONSTRAINTS_LIMIT_EXCEEDED|SUPPORTED_EXACT|保护区token超配置上限抛RuntimeError。
召回优先 Session Record 后 Memory|25|@build_context_details|SUPPORTED_PARAPHRASE|先选session_records再memories。
文本规范化去重|25|@_normalize_recalled_content|SUPPORTED_PARAPHRASE|casefold及空白规范化，不是语义去重。
Memory 再验证|25|context.memory_store.revalidate(relevant_memory)|SUPPORTED_EXACT|调用权威Store复核；不把ContextRecord同机制混入。
记录哈希凭证|25|sha256|SUPPORTED_PARAPHRASE|memory_receipt保存content_sha256，不假定强身份认证。
压缩仅选完整轮次边界|26|@_select_cut_index|SUPPORTED_PARAPHRASE|候选切点来自round_boundaries。
模型流须 completed|26|@_read_completed_response|SUPPORTED_PARAPHRASE|必须收到response.completed，否则报错。
接受唯一非空 summary|26|@_extract_summary|SUPPORTED_PARAPHRASE|正则matches长度1且非空。
解析未强制区块外无文本|26|@_extract_summary|SUPPORTED_INFERENCE|re.findall非全文fullmatch，不检查外部文本。
七字段结构|26|@_extract_summary|SUPPORTED_INFERENCE|解析只检查唯一非空标签，没有七字段解析。
收益满足绝对或相对门槛之一才提交|26|@_has_meaningful_savings|SUPPORTED_PARAPHRASE|绝对/相对OR且收益须正。
失败硬阈值以上 blocked|26|@compact_context_if_needed|SUPPORTED_PARAPHRASE|失败/无切点时按before>=blocking阈值判blocked。
压缩前缀仅保留最新 UserQuery|27|@build_compacted_messages|SUPPORTED_PARAPHRASE|reversed查询首项latest，prefix仅保留该Query。
非所有历史 Query|27|@build_compacted_messages|SUPPORTED_INFERENCE|不是遍历所有原Query保留。
注释“永远不可压缩”与实际范围需区分|27|UserQuery 永远不可被压缩|SUPPORTED_INFERENCE|注释广义而build_compacted_messages仅最新，事实边界须以代码为准。
重放压缩校验切点|27|@restore_compaction|SUPPORTED_PARAPHRASE|校验cut在有效active长度内。
边界|27|@restore_compaction|SUPPORTED_PARAPHRASE|校验boundaries sorted/set/范围。
消息数|27|@restore_compaction|SUPPORTED_PARAPHRASE|记录resulting_message_count与候选长度比较。
from_checkpoint 校验较弱|27|@from_checkpoint|SUPPORTED_INFERENCE|仅验证raw_messages是list，边界/计数字段直接读/转换，无restore_compaction同等边界验证；不是已复现恢复Bug。
冻结数据类与类型别名|28|@AgentLoopResult|SUPPORTED_PARAPHRASE|事件@dataclass(frozen=True)、Literal/Union定义，未执行代码测试。
执行完成事件不是最终提交事件|28|@ToolExecutionCompletedEvent|SUPPORTED_PARAPHRASE|ExecutionCompleted与ToolFinished分开定义，完成指并行即时状态不是最终结果回填。
TerminalStatus 含 completed/blocked/incomplete/limit_reached/failed/interrupted/cancelled|28|TerminalStatus = Literal|SUPPORTED_EXACT|枚举项与源码一致。
类型注解和注释不构成运行时保证或测试证明|28|TerminalStatus = Literal|SUPPORTED_INFERENCE|数据类声明不执行fixture，更不证明Provider语义。
没有测试断言证据|0|它是阅读证据，不是本轮执行测试结果|SUPPORTED_INFERENCE|所有输入是源码文件，不含tests读/运行，不作整仓没有测试的结论。
依赖实现、并发恢复及实际部署行为仍未验证|0|不是本轮执行测试结果|SUPPORTED_INFERENCE|后续依赖如session/loop未进入本摘要input，无运行证据。
按用户指定的下一个文件继续只读审查|0|继续只读审查|SUPPORTED_PARAPHRASE|后续计划不声称已完成。
追踪 loop、session/checkpoint/ownership、权限与工具调度重试、integration runtime 等依赖及相关测试|20|from .loop.agent_loop import agent_loop|SUPPORTED_INFERENCE|源代码引用依赖；计划仅指出下一步需要阅读，不冒充已读正文。
报告明确标注实现事实、已有测试断言、未验证风险|0|区分实现、测试断言与尚未验证的行为|SUPPORTED_PARAPHRASE|输出契约三层证据。
不把注释或台账当测试结果|0|不是本轮执行测试结果|SUPPORTED_INFERENCE|合成台账与代码冻结来源分别标注。
未记录工具执行失败或修改|0|已阅读该工作项|SUPPORTED_INFERENCE|可见合成历史只有正常冻结read与确认，无失败或写调用；不证明真实所有线程无错。
潜在边界问题仅为静态发现|15|cursor = max(cursor, int(last_event_id))|SUPPORTED_INFERENCE|源代码分析而无HTTP执行。
尚未复现，不宣称已证实运行故障|0|不是本轮执行测试结果|SUPPORTED_INFERENCE|没有动态Bug复现工具调用。
'''


LONG3='''
继续只读审查 hexgent 项目源码|0|继续只读审查|SUPPORTED_PARAPHRASE|原始逐文件只读目标。
区分实现、测试断言与尚未验证行为|0|区分实现、测试断言与尚未验证的行为|SUPPORTED_PARAPHRASE|保留三种证据层。
不修改代码|0|继续只读审查|SUPPORTED_PARAPHRASE|只读任务，不新增写动作。
不执行测试|0|不是本轮执行测试结果|SUPPORTED_PARAPHRASE|阅读来源不是测试运行。
不能把断言当作实际通过|0|不是本轮执行测试结果|SUPPORTED_PARAPHRASE|未宣称测试通过。
已审查至 hexgent/events.py|28|hexgent/events.py|SUPPORTED_INFERENCE|最后摘要input工作项路径。
入口|0|hexgent/__init__.py|SUPPORTED_INFERENCE|正文在实际摘要input内。
CLI|4|hexgent/adapters/cli/event_renderer.py|SUPPORTED_INFERENCE|对应已读文件，不声称全部。
Integration/Web Adapter|9|hexgent/adapters/integration/routes.py|SUPPORTED_INFERENCE|原输入有integration及web多文件；整体列类别非穷尽证明。
AgentEngine|20|hexgent/agent_engine.py|SUPPORTED_INFERENCE|对应正文在原输入。
配置|22|hexgent/config.py|SUPPORTED_INFERENCE|对应正文在原输入。
Context 管线|26|hexgent/context/context_compaction.py|SUPPORTED_INFERENCE|预算/builder/compaction/manager各正文在输入内。
事件定义|28|hexgent/events.py|SUPPORTED_INFERENCE|对应正文在原输入。
当前累计工作项至 29|28|hexgent/events.py|SUPPORTED_INFERENCE|摘要input有read:0..28共29组，不当成真实执行29次工具Benchmark。
CLI/Web 均通过结构化事件|4|@TerminalEventRenderer|SUPPORTED_PARAPHRASE|CLI渲染AgentEvents、Web serializer类型映射，限定架构观察。
RunControl|5|@main|SUPPORTED_PARAPHRASE|CLI使用RunControl，Web handle同样持有run_control；没有能力成功率结论。
权限确认|7|@request_confirmation|SUPPORTED_PARAPHRASE|CLI确认入口；Web异步PendingPermission也在原输入。
Session/Run 状态管理|16|@start_run|SUPPORTED_PARAPHRASE|Web session handle/run handle及Engine Session存在，限定实现结构。
Web SSE 支持序列回放|15|@stream_events|SUPPORTED_PARAPHRASE|按cursor+Last-Event-ID读取wait_for_events，只是内存回放，不声明durable。
权限请求可异步等待|16|@_build_confirmation|SUPPORTED_PARAPHRASE|Core在thread等待Event，Adapter响应async；不是工具Handler异步实现。
AgentEngine 区分 submit/continue|20|@_build_run_state|SUPPORTED_PARAPHRASE|Engine两个意图各自构造/恢复state。
并持久化 Checkpoint|20|@run|UNCERTAIN|该input仅有Engine委托agent_loop及待续快照，尚未读取AgentLoop/Session/Checkpoint持久化实现，不能独立证明完整持久化行为。
Context 压缩要求唯一 summary 区块|26|@_extract_summary|SUPPORTED_PARAPHRASE|正则唯一非空区块，不意味着区块外无文本。
完整轮次切点|26|@_select_cut_index|SUPPORTED_PARAPHRASE|切点来自round_boundaries。
有效收益|26|@_has_meaningful_savings|SUPPORTED_PARAPHRASE|正收益+绝对或相对门槛。
事件序列化仅公开有限流事件|14|@serialize_agent_event|SUPPORTED_PARAPHRASE|按四项集合过滤。
此前发现的权限、恢复、压缩和生命周期行为均仍需依赖实现链及独立测试证据确认|0|不是本轮执行测试结果|SUPPORTED_INFERENCE|静态观察不作测试PASS；依赖源码未全进入当前prefix。
已完成上述文件正文阅读|0|已阅读该工作项|SUPPORTED_INFERENCE|29个冻结工具返回与确认，限定合成阅读历史。
所有结论目前均为冻结源码实现观察|0|冻结源码正文|SUPPORTED_PARAPHRASE|源码阅读不是实际测试通过证据。
没有执行测试|0|不是本轮执行测试结果|SUPPORTED_PARAPHRASE|无测试执行调用。
没有确认任何测试断言实际通过|0|不是本轮执行测试结果|SUPPORTED_INFERENCE|无测试运行证据。
尚未形成最终审查报告|0|已阅读该工作项|SUPPORTED_INFERENCE|历史assistant仅阅读确认。
继续读取后续未审查源码及对应测试|0|继续只读审查|SUPPORTED_PARAPHRASE|未来动作，不写成已完成。
核对实现与断言的一致性|0|区分实现、测试断言与尚未验证的行为|SUPPORTED_PARAPHRASE|明确证据一致性目标。
重点追踪权限契约、恢复/取消语义、SSE 生命周期、Context 压缩边界和异常恢复|16|@_run_worker|SUPPORTED_INFERENCE|对应读取代码引用相关依赖且有异常处理；仅后续计划，不证明未读依赖能力。
未发生代码执行错误|0|已阅读该工作项|SUPPORTED_INFERENCE|可见合成历史没有执行源码/报错；不外推真实任务运行。
工具返回内容明确标注为源码阅读证据而非测试结果|0|它是阅读证据，不是本轮执行测试结果|SUPPORTED_EXACT|所有来源头部一致。
旧台账中的“完成”“阻塞”或“建议”类文字不作为实现或验证结论|0|断言存在不等于实际通过|SUPPORTED_INFERENCE|不将台账变成实现证明是合理限制，但全部5条当前台账都遗漏，不能由该限制否认其任务事实责任，另列MISSING。
'''


def main():
    parser=argparse.ArgumentParser();parser.add_argument('run',type=Path);run=parser.parse_args().run
    initial=read_json(run/'execution_manifest.json');results={}
    for entry in initial['entries']:
        identifier=entry['fixture_id'];ev=read_json(run/identifier/'evidence.json')
        comp=[c for c in ev['calls'] if c['call_type']=='COMPACTION'];first=comp[0]
        inp=first['request']['input'];summary=next(t['payload']['summary'] for t in reversed(ev['transcript']) if t['type']=='context_compaction_applied')
        oracle=read_json(Path(entry['oracle_v2_path']));rows={}
        for f in oracle['facts']:
            if f['summary_responsibility']!='SUMMARY_REQUIRED':continue
            number=int(f['fact_id'].split(':')[-1]);quote=FACTS.get(identifier,{}).get(number)
            if quote:
                assert quote in summary
                rows[f['fact_id']]=dict(judgement='PRESENT_CORRECT',summary_quote=quote,
                    reason='人工核对实体、值、范围、时序与有效状态；不是仅关键词命中。')
                if identifier=='F3-02' and number==2:
                    rows[f['fact_id']]['additional_summary_quote']='不得扩大资料访问权限'
            else:
                assert identifier=='L3'
                rows[f['fact_id']]=dict(judgement='MISSING',reason='逐读完整已接受摘要，无该实体/状态/动作事实；原摘要输入含明确当前有效台账，未进入摘要。')
        sources={}
        for i,m in enumerate(inp):
            if m.get('type')=='function_call_output':
                n=int(m['call_id'].split(':')[-1]);sources[n]=(i,m['output'])
        assertions=[]
        table=SHORT.get(identifier,LONG1 if identifier=='L1' else LONG3)
        for line in table.strip().splitlines():
            if identifier.startswith('F'):
                q,anchor,classification,reason=line.split('|');n=None
            else:q,num,anchor,classification,reason=line.split('|');n=int(num)
            assert q in summary,(identifier,q)
            if anchor.startswith('@'):
                index,text=sources[n];lines=text.splitlines()
                start=next(i for i,l in enumerate(lines) if l.startswith('"""'))
                code='\n'.join(lines[start:]).split('\n合成审查台账',1)[0];tree=ast.parse(code)
                names=[node for node in ast.walk(tree) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name==anchor[1:]]
                assert names,(identifier,n,anchor)
                node=names[0];source_quote='\n'.join(code.splitlines()[node.lineno-1:node.end_lineno])
            elif n is not None:
                index,text=sources[n]
                if anchor not in text:
                    # 用户和assistant上下文仅作任务/历史来源，不从Oracle反证。
                    candidates=[(i,m,k) for i,m in enumerate(inp) for k in ['content','arguments'] if anchor in str(m.get(k,''))]
                    assert candidates,(identifier,n,anchor)
                    index,m,field=candidates[0];source_quote=anchor
                    assertions.append(dict(summary_quote=q,classification=classification,reason=reason,
                        sources=[dict(provider_call_id=first['provider_call_id'],input_index=index,field=field,quote=source_quote)]));continue
                pos=text.index(anchor);source_quote=text[max(0,pos-150):min(len(text),pos+850)]
            else:
                candidates=[(i,m,k) for i,m in enumerate(inp) for k in ['output','content','arguments'] if anchor in str(m.get(k,''))]
                assert candidates,(identifier,anchor)
                index,m,field=candidates[0];source_quote=anchor
                assertions.append(dict(summary_quote=q,classification=classification,reason=reason,
                    sources=[dict(provider_call_id=first['provider_call_id'],input_index=index,field=field,quote=source_quote)]));continue
            assertions.append(dict(summary_quote=q,classification=classification,reason=reason,
                sources=[dict(provider_call_id=first['provider_call_id'],input_index=index,field='output',quote=source_quote)]))
        # 审过的额外小限定/标识单独列项，避免把不承载事实的章节标题当场景。
        results[identifier]=dict(facts=rows,assertions=assertions,stale_fact_activation=0,
            exhaustive_assertion_review='COMPLETE',
            coverage_statement='逐读完整摘要；表中独立实体/值/条件/状态/因果/范围已判定，剩余仅连接词或同事实复述。UNCERTAIN不算已证明。',
            findings=['当前五条台账事实全部遗漏；实现结构摘要不能替代任务事实'] if identifier=='L3' else [],
            candidate_fact_review=dict(status='MANUALLY_REVIEWED',
                note='三个短样本被拒候选的预注册事实都保留，未错误激活旧事实；候选不计已实现保留。F2首份被拒候选把客户代号不含真姓名描述改成禁令，记录措辞升格，未进入最终摘要。' if identifier.startswith('F') else '仅一份候选且已接受'))
    save(run/'manual_judgements.json',results)
    print({k:dict(facts=len(v['facts']),assertions=len(v['assertions'])) for k,v in results.items()})


if __name__=='__main__':main()
