"""Stage 1 离线人工审查固化；只用已有文件，无 SDK/Provider 调用入口。"""
import argparse
import ast
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import socket
import subprocess


REPO = Path(__file__).resolve().parents[3]
RUN = REPO/'eval/context_management/c2/runs/stage1-real-20261011T150121165142Z'


# 逐条人工语义判定。列：原始输入中的文件序号、摘要原文、来源测试方法、判定、理由。
# 不是关键词分类器；定位失败即停止，不能把来源外的预期答案补作证据。
CLAIMS = '''
0|显式配置段覆盖|test_run_options_only_override_explicit_sections|SUPPORTED_PARAPHRASE|同一fixture仅覆盖loop，其余context/tool_parallel对象保持原配置。
0|Session 权限与配置分离|test_agent_engine_separates_config_dependencies_and_state|SUPPORTED_PARAPHRASE|配置default与Session plan分别断言，不混为一次权限放行。
0|惰性流未消费不追加 Query|test_unconsumed_stream_does_not_append_query|SUPPORTED_PARAPHRASE|构造未消费generator后user_queries为空。
0|并发第二次消费报“已有正在执行”|test_concurrent_run_is_rejected_before_appending_query|SUPPORTED_PARAPHRASE|首次流仍活动时消费第二流抛指定异常。
0|且不追加 Query|test_concurrent_run_is_rejected_before_appending_query|SUPPORTED_PARAPHRASE|失败后查询列表只含任务A；限该Engine并发场景。
0|非法配置 ConfigError|test_invalid_base_and_run_override_configs_are_rejected|SUPPORTED_PARAPHRASE|base/override非法配置都有异常断言。
0|continue 恢复 model_rounds=3|test_continue_reuses_checkpoint_state_without_appending_query|SUPPORTED_EXACT|恢复state.model_rounds明确等于3。
0|tool_calls=5|test_continue_reuses_checkpoint_state_without_appending_query|SUPPORTED_EXACT|恢复state.tool_calls明确等于5。
0|不追加 Query|test_continue_reuses_checkpoint_state_without_appending_query|SUPPORTED_PARAPHRASE|continue后user_queries为空；与并发场景分别归属。
0|submit 清旧快照|test_submit_supersedes_pending_checkpoint_run|SUPPORTED_PARAPHRASE|新submit后_pending_run_state为None。
0|取消清 Plan|test_cancel_pending_task_clears_retained_plan|SUPPORTED_PARAPHRASE|cancel_pending_task后plan_store为空。
1|流式最终文本只打印一次|test_final_text_is_not_printed_again_on_run_finished|SUPPORTED_PARAPHRASE|输出你好世界计数1；不是证明真实Provider所有流行为。
1|checkpoint cp-000003 显示工具批次回填原因|test_checkpoint_commit_is_rendered_with_explicit_reason|SUPPORTED_PARAPHRASE|明确断言cp-000003及工具批次结果已回填文案。
1|/restore session-v2 不调 agent_loop|test_direct_restore_command_replaces_engine_without_model_run|SUPPORTED_PARAPHRASE|restore参数session-v2、stream.assert_not_called相互支持。
1|同 c1 即时完成/顺序提交不重复显示|test_parallel_execution_completion_is_not_printed_twice|SUPPORTED_PARAPHRASE|同c1两种完成事件、最终显示次数1；未外推所有并发结果。
1|预算|test_limit_result_returns_to_next_user_input|SUPPORTED_PARAPHRASE|max_total_tokens收尾后再次接收输入，两次stream调用。
1|incomplete|test_incomplete_plan_message_does_not_end_session|SUPPORTED_EXACT|incomplete终态后第二轮继续，未结束CLI。
1|interrupted 后接收下一输入|test_interrupted_run_prints_hint_and_returns_to_input|SUPPORTED_PARAPHRASE|interrupted终态后第二次stream可运行。
2|预算转 Continue/budget_finalization/finalizing|test_budget_limit_returns_continue_with_finalization_state|SUPPORTED_PARAPHRASE|同一断言包含Continue、reason与mode，完整引用方法范围。
2|收尾请求无 tools|test_round_limit_reserves_one_finalization_response|SUPPORTED_PARAPHRASE|第二调用无tools；第一调用有tools。
2|max_tool_calls=1 时 c1/c2/c3 全配对|test_every_tool_call_gets_a_result_after_tool_limit|SUPPORTED_PARAPHRASE|一个允许调用与两个未执行结果均保留原call_id集合。
2|后两项“未执行”|test_every_tool_call_gets_a_result_after_tool_limit|SUPPORTED_PARAPHRASE|第二及第三output断言含未执行，未称真实Handler都执行。
2|活动 Plan 提醒|test_active_plan_nudge_constructs_explicit_next_state|SUPPORTED_PARAPHRASE|一次nudge构造Continue并附不能直接结束提示。
2|未完成保留|test_active_plan_after_nudges_returns_incomplete_and_is_retained|SUPPORTED_PARAPHRASE|提醒耗尽incomplete且Plan对象仍原对象。
2|blocked 保留|test_blocked_plan_returns_blocked_terminal_and_is_retained|SUPPORTED_PARAPHRASE|blocked终态及原Plan对象保留均断言。
2|completed 清除|test_completed_plan_is_cleared_before_normal_stop|SUPPORTED_PARAPHRASE|completed前get_plan为空。
2|主动压缩转 context_compacted|test_context_compaction_is_an_explicit_loop_transition|SUPPORTED_PARAPHRASE|事件包含转换、compaction_count=1。
2|并清缓存|test_context_compaction_is_an_explicit_loop_transition;test_context_compaction_clears_exact_tool_call_cache|SUPPORTED_PARAPHRASE|read_file_cache和精确调用缓存各自有独立断言，不合并为一次实测。
2|过长异常应急压缩仅重试一次|test_prompt_too_long_triggers_one_reactive_compaction_retry|SUPPORTED_PARAPHRASE|一失败主请求、一摘要、一成功主请求；只断言该fixture路径。
2|第二次过长 failed|test_repeated_prompt_too_long_stops_without_second_reactive_retry|SUPPORTED_PARAPHRASE|另一个双过长异常fixture断言failed，不混入前一成功例。
2|残缺流不执行工具|test_incomplete_stream_never_executes_tool|SUPPORTED_PARAPHRASE|工具execute未调用，残缺arguments不进入执行。
2|失败流终态 failed|test_failed_stream_returns_error_terminal|SUPPORTED_EXACT|最后RunFinished.result.status=failed。
2|文本顺序断言不证明即时性|test_text_deltas_are_emitted_in_order|SUPPORTED_INFERENCE|断言只比较最终收集的delta顺序，没有实时计时Oracle。
2|开始/结束事件断言不完整证明执行时序|test_tool_events_wrap_execution_after_completed_response|SUPPORTED_INFERENCE|Handler日志仅execute一次，事件收集后只比较类型/Call ID，没有Handler与事件的联合时序；是证据限制非实现结论。
3|call_model 两例仅覆盖空流/输出前 incomplete chunked read 后一次成功重试|test_retries_empty_stream_once_before_returning_completed_response;test_retries_connection_failure_before_any_output|SUPPORTED_PARAPHRASE|两个独立测试分别空流和create异常，第二流完成。
3|未单独验证已有输出不重试|*|SUPPORTED_INFERENCE|完整文件仅上述两方法，无已有输出后失败fixture。
3|或重试再失败|*|SUPPORTED_INFERENCE|两个重试均成功，没有第二次失败断言。
5|完整轮次安全边界|test_tool_call_and_result_are_sent_to_summary_together|SUPPORTED_PARAPHRASE|completes_round边界，调用和结果同时送摘要。
5|call-1 调用与结果共同摘要|test_tool_call_and_result_are_sent_to_summary_together|SUPPORTED_EXACT|call_id=call-1，summary_input同时含call/result对象。
5|最新 Query 保留|test_active_context_keeps_latest_query_after_compaction|SUPPORTED_PARAPHRASE|model_messages中user内容仅保留最新second Query。
5|无边界零请求|test_no_safe_boundary_reports_failure_without_changing_messages|SUPPORTED_PARAPHRASE|只有长user消息无完整边界，calls=[]。
5|且 blocked|test_no_safe_boundary_reports_failure_without_changing_messages|SUPPORTED_EXACT|对应outcome.status=blocked，不与软阈值场景混用。
5|无收益摘要两次后 blocked|test_ineffective_summary_retries_then_blocks_without_mutation|SUPPORTED_PARAPHRASE|max_attempts=2，calls=2、blocked。
5|不变更上下文|test_ineffective_summary_retries_then_blocks_without_mutation|SUPPORTED_PARAPHRASE|active_messages与原列表相等、compaction_count=0。
5|预算记录|test_compaction_returns_boundary_and_preserves_recent_messages|SUPPORTED_PARAPHRASE|request_budget分项与boundary副本有断言。
5|摘要 usage 有断言|test_compaction_returns_boundary_and_preserves_recent_messages|SUPPORTED_PARAPHRASE|Fake usage输入20输出8有独立outcome断言，未称Provider真实用量。
6|提取校验来源/原文|test_source_and_quote_validation|SUPPORTED_PARAPHRASE|合法候选与无原文候选分别校验。
6|未来证据|test_source_and_quote_validation|SUPPORTED_PARAPHRASE|source_sequences=[999]触发ValueError。
6|模型自述约束拒绝|test_source_and_quote_validation|SUPPORTED_PARAPHRASE|引用模型已了解来源的约束候选触发ValueError。
6|提交失败不提前改内存|test_failed_commit_does_not_mutate_memory|SUPPORTED_PARAPHRASE|append抛OSError后records=空。
6|游标|test_failed_commit_does_not_mutate_memory|SUPPORTED_PARAPHRASE|同提交失败fixture中last_extracted_sequence=0。
6|空提交可恢复游标|test_commit_and_empty_commit_restore_together|SUPPORTED_PARAPHRASE|空upsert推进next_through，正式恢复游标一致。
6|正常结束不阻塞提取|test_loop_does_not_block_normal_finish_for_extraction|SUPPORTED_PARAPHRASE|patched正常Stop后extraction未调用，不表示其它边界异步。
6|失败后同 Run 抑制|test_failed_extraction_is_suppressed_until_next_run|SUPPORTED_PARAPHRASE|空流后suppressed=True，下一次extract返回(0,0)。
6|次 Run 重置未在该例证明|test_failed_extraction_is_suppressed_until_next_run|SUPPORTED_INFERENCE|方法没有新Run步骤，名称不能补作行为证据。
8|持久化合并|test_upsert_persists_projection_and_replays_from_transcript|SUPPORTED_PARAPHRASE|同事实updated并合并来源(3,4)，projection和restorer一致。
8|替代|test_upsert_persists_projection_and_replays_from_transcript|SUPPORTED_PARAPHRASE|旧superseded、新active数量1的断言。
9|快照召回有断言|test_engine_retrieves_records_and_context_builder_injects_them|SUPPORTED_PARAPHRASE|relevant_context召回、Builder及RunStateSnapshot往返比较；Agent Loop是patch，非真模型结果。
7|Vorus-Q9 A=48317|$A|SUPPORTED_EXACT|源码常量A实体与端口一致。
7|B=59263|$B|SUPPORTED_EXACT|源码常量B实体与端口一致。
7|C=60113|$C|SUPPORTED_EXACT|源码常量C实体与端口一致。
7|恢复后只注入当前有效记录|test_r1_corrected_checkpoint_selects_b|SUPPORTED_PARAPHRASE|pause/correct/resume/assert_only(B)经助手方法核查，限显式更正。
7|链替代仅 C|test_r3_chained_supersede_only_c|SUPPORTED_PARAPHRASE|A到B到C后assert_only(C)。
7|三次请求不复活 A|test_r4_multiple_requests_do_not_revive_a|SUPPORTED_PARAPHRASE|requests长度3且assert_only(B)遍历三请求。
7|空流重试首请求 A|test_r5_empty_stream_retry_reselects_after_correction|SUPPORTED_PARAPHRASE|第一请求record_block内容[A]。
7|纠正后第二请求 B|test_r5_empty_stream_retry_reselects_after_correction|SUPPORTED_PARAPHRASE|空流期间更正后第二请求[B]。
7|关闭检索无注入|test_r6_continue_disabled_does_not_inject_cached_record|SUPPORTED_PARAPHRASE|OFF配置Continue后Record区块为空。
7|ON/OFF 保留相同普通摘要历史|test_nonempty_summary_on_off_retains_normal_history|SUPPORTED_PARAPHRASE|requests.input相同，OFF保留历史A但Record区块为空，区分历史渠道。
7|均 FakeModel|*|SUPPORTED_PARAPHRASE|fixture模型来自eval.support.FakeModel；不代表真实模型效果。
7|本地机制证据|*|SUPPORTED_PARAPHRASE|本地TemporaryDirectory、暂停/恢复和请求断言。
7|不是模型效果验收|*|SUPPORTED_INFERENCE|固定输出不能证明模型正确使用事实。
14|显式去重|test_explicit_memory_is_readable_and_deduplicated|SUPPORTED_PARAPHRASE|remember两次成功、单条目并合并关键词。
14|Markdown 迁移|test_legacy_single_file_is_migrated_to_authoritative_items|SUPPORTED_PARAPHRASE|MEMORY.md迁移到items路径，内容与处理游标保留。
14|Session Record 压制重复 Memory|test_context_record_suppresses_duplicate_memory_in_instructions|SUPPORTED_PARAPHRASE|同文本ContextRecord在、Memory区块不在。
14|召回预算剔除|test_runtime_injection_budget_drops_retrieval_before_fixed_context|SUPPORTED_PARAPHRASE|runtime预算不足两类召回区块被剔除。
14|固定 Skill 不截断|test_oversized_active_skill_records_warning_without_truncation|UNCERTAIN|只assertIn一个规则片段、warning和估算Token>1，没有与完整active_skill正文相等/包含的断言；docstring不能补作全文未截断证明。
15|生命周期替代|test_single_and_chained_supersede|SUPPORTED_PARAPHRASE|单次/链式supersede当前items及权威SUPERSEDED状态断言。
15|撤销|test_invalidate_idempotent_and_no_resurrection|SUPPORTED_PARAPHRASE|invalidate重复revision不变、重建items为空。
15|删除墓碑|test_delete_body_and_tombstone_survive_restart|SUPPORTED_PARAPHRASE|body消失、权威正文移除、重建为空且同ID upsert拒绝REVOKED。
15|跨进程竞争|test_supersede_delete_cross_process_race|SUPPORTED_PARAPHRASE|spawn双进程，delete成功，旧ID不再active；不推导Exactly-once。
15|请求与重试前重校验|test_actual_request_rechecks_after_context_was_built;test_model_retry_rechecks_revocation|SUPPORTED_PARAPHRASE|分别已组装后撤销和空流重试前撤销，真实FakeModel请求不含旧事实。
15|checkpoint continue|test_formal_checkpoint_restore_continue_revalidates|SUPPORTED_PARAPHRASE|has_continuable_run、restore、RunRequest.continue_run及后续instructions无旧事实。
15|权限|test_lifecycle_tools_production_pipeline_and_permission|SUPPORTED_PARAPHRASE|deny保留item、allow撤销的两次工具调用独立断言。
15|缓存撤销|test_remember_cache_cannot_bypass_external_revocation|SUPPORTED_PARAPHRASE|先成功再deduplicated，外部撤销后MEMORY_REVOKED。
15|提交前|test_uncommitted_new_body_is_not_retrievable|SUPPORTED_PARAPHRASE|commit前OSError后Store仅旧事实。
15|后故障有源码断言|test_post_commit_index_failure_does_not_restore_old_fact|SUPPORTED_PARAPHRASE|commit后index失败，重建Store仍新事实，不等于本轮已运行测试。
16|session=None 与有 Session 都校验最终 instructions/input/tools|test_normal_request_dispatches_unchanged;test_session_normal_and_rejection_contract_is_unchanged;test_tool_schema_is_included_in_final_budget|SUPPORTED_PARAPHRASE|两条路径分别验证正常/拒绝；with_tools与without_tools比较证明schema参与预算。
16|实际 max_output_tokens|test_output_allowance_and_safety_margin_are_included|SUPPORTED_PARAPHRASE|请求actual输出8000与候选窗口分别设置相差1。
16|margin|test_output_allowance_and_safety_margin_are_included|SUPPORTED_PARAPHRASE|安全余量100，窗口差99时也拒绝。
16|超窗零发送|test_original_combined_over_budget_reproduction_has_zero_dispatch|SUPPORTED_EXACT|calls=[]，不是送出后Provider拒绝。
16|failed/CONTEXT_BUDGET_EXCEEDED|test_original_combined_over_budget_reproduction_has_zero_dispatch|SUPPORTED_PARAPHRASE|同fixture终态failed及output错误码。
16|空流重试每次真实估算|test_retry_rechecks_real_budget_before_each_dispatch|SUPPORTED_PARAPHRASE|两次calls对应两次spy真实estimator，参数相等并分别核预算。
16|文件明确是局部修复回归、非正式 C1 验收|*|SUPPORTED_EXACT|文件docstring明确该边界，不从fixture通过推断正式C1结果。
10|严格仅 parentFolderId/title/content|test_schema_exposes_only_create_contract|SUPPORTED_PARAPHRASE|properties/required三字段、strict=True/additionalProperties=False。
10|workspace-run-42/model-call-7 原样传 invoke|test_success_preserves_workspace_run_and_original_call_identity|SUPPORTED_PARAPHRASE|invoke.assert_called_once_with完整参数含同run/call标识。
10|deny 不调用客户端|test_workspace_write_permission_can_deny_before_client|SUPPORTED_PARAPHRASE|denied/user_denied且invoke未调用。
10|WS DENIED 为 permission|test_ws_denied_is_model_visible_permission_failure|SUPPORTED_PARAPHRASE|WS DENIED转failure.phase=permission且一次attempt。
10|DOCUMENT_CREATE_PENDING|test_pending_and_idempotency_conflict_are_visible_without_auto_retry|SUPPORTED_EXACT|单独subTest对应code，attempt_count=1。
10|IDEMPOTENCY_CONFLICT 单次尝试|test_pending_and_idempotency_conflict_are_visible_without_auto_retry|SUPPORTED_PARAPHRASE|另一个subTest一次invoke，无自动重试。
10|伪造权威字段 invalid|test_rejects_forged_authority_and_internal_fields_before_client|SUPPORTED_PARAPHRASE|userId/workspaceId/runId/callId伪造后invalid_arguments。
10|不调用客户端|test_rejects_forged_authority_and_internal_fields_before_client|SUPPORTED_EXACT|上述伪造字段fixture invoke.assert_not_called。
11|lifecycle 单例|*|SUPPORTED_EXACT|该文件一个test方法，不代表Production所有生命周期仅此。
11|R1 内 A failed 后 B resume 投影 Started/Failed/Resumed|test_failed_run_is_deterministic_and_run_scoped|SUPPORTED_PARAPHRASE|输入workspace_run_id=R1、A failed、ResumeRun/B，输出事件三个对应。
11|序号1/2/3|test_failed_run_is_deterministic_and_run_scoped|SUPPORTED_EXACT|输出事件sequence=[1,2,3]，不混淆输入Transcript seq1..5。
11|event_id确定|test_failed_run_is_deterministic_and_run_scoped|SUPPORTED_PARAPHRASE|同records两次project比较event_id列表相等。
11|MODEL_ERROR|test_failed_run_is_deterministic_and_run_scoped|SUPPORTED_EXACT|Failed.payload.failureCode明确MODEL_ERROR。
12|直接子项 a.txt/folder/link|test_lists_direct_children_and_filters_hidden|SUPPORTED_PARAPHRASE|建有nested.txt但返回仅三个直接子项。
12|symlink 标记|test_lists_direct_children_and_filters_hidden|SUPPORTED_EXACT|link.kind=symlink。
12|隐藏过滤|test_lists_direct_children_and_filters_hidden|SUPPORTED_PARAPHRASE|include_hidden=False列表无hidden，True包含hidden。
12|截断|test_lists_direct_children_and_filters_hidden|SUPPORTED_PARAPHRASE|max_entries=1时truncated=True，正常False。
12|max_entries=0 invalid|test_invalid_limit_and_outside_permission|SUPPORTED_EXACT|无效限制result.status=invalid。
12|父目录用户拒绝 denied|test_invalid_limit_and_outside_permission|SUPPORTED_PARAPHRASE|path=..、用户deny后status=denied。
13|回调一次|test_controller_invokes_registered_callback_once|SUPPORTED_PARAPHRASE|两次interrupt后invocations仅一项closed。
13|信号入口不等待阻塞回调|test_signal_interrupt_does_not_wait_for_blocking_callback|UNCERTAIN|回调wait(timeout=1)，随后仅检查interrupted和started；假设入口同步等到1秒超时也能通过，没有耗时或回调尚未结束的断言，不能严格证明非阻塞。
13|入口中断零模型请求|test_interrupt_at_turn_entry_does_not_call_model|SUPPORTED_EXACT|responses.calls=[]。
13|流中断只展示 Delta|test_interrupt_during_stream_does_not_commit_partial_response|SUPPORTED_PARAPHRASE|deltas仅已经展示。
13|不提交 partial|test_interrupt_during_stream_does_not_commit_partial_response|SUPPORTED_PARAPHRASE|active_messages=[]，不提交completed伪完整结果。
13|工具取消 write-1 success|test_interrupt_during_tools_pairs_every_tool_call|SUPPORTED_EXACT|mock第一调用成功，finished_statuses.write-1=success；不是实际文件写入证据。
13|read-2 error 均配对|test_interrupt_during_tools_pairs_every_tool_call|SUPPORTED_PARAPHRASE|第二未执行中断结果，call_id顺序匹配且状态error。
13|cancelled/user_cancel|test_interrupt_during_tools_pairs_every_tool_call|SUPPORTED_EXACT|取消工具fixture终态及stop_reason分别对应。
13|暂停 interrupted/user_pause|test_interrupt_at_turn_entry_does_not_call_model|SUPPORTED_EXACT|另一入口pausefixture status/stop_reason，不合并作同一运行。
13|日志末尾 finished/checkpoint|test_session_records_interrupt_boundary_and_terminal|SUPPORTED_PARAPHRASE|最后两条type agent_run_finished/checkpoint_committed，finished.status=interrupted。
13|取消清 Plan|test_cancel_clears_active_plan_but_records_snapshot|SUPPORTED_EXACT|plan_store.get_plan=None。
13|且保存快照|test_cancel_clears_active_plan_but_records_snapshot|SUPPORTED_PARAPHRASE|plan_cancelled payload保存旧objective，不暗示回滚副作用。
13|steer FIFO|test_steering_queries_are_applied_fifo_inside_same_run|SUPPORTED_PARAPHRASE|user_queries顺序两条steer，并继续同Run。
17|workspace read allow/allow_rule/workspace_read|test_workspace_read_is_allowed_by_explicit_rule|SUPPORTED_EXACT|read_file(a.txt)三个decision字段精确断言。
17|default write ask/default_fallback|test_default_mode_asks_for_workspace_write|SUPPORTED_EXACT|default write_file decision行为/来源精确匹配。
17|accept_edits write allow/permission_mode|test_accept_edits_mode_allows_workspace_write|SUPPORTED_EXACT|写权限行为及来源按该mode断言。
17|plan 禁 write/bash|test_plan_mode_denies_write_and_shell|SUPPORTED_PARAPHRASE|同mode两独立evaluation均deny。
17|允许 read|test_plan_mode_denies_write_and_shell|SUPPORTED_EXACT|read_decision.behavior=allow。
17|dont_ask unresolved deny/dont_ask_fallback|test_dont_ask_denies_unresolved_and_explicit_ask_calls|SUPPORTED_EXACT|pwd unresolved调用行为/来源匹配。
17|外部 read deny/outside_workspace_access|test_dont_ask_denies_unresolved_and_explicit_ask_calls|SUPPORTED_EXACT|../outside.txt对应deny及rule_id。
17|auto_approve 仅跳 ask|test_auto_approve_allows_ask_but_not_hard_deny|SUPPORTED_PARAPHRASE|write允许auto_approve_mode但硬拒绝保留；仅该例，未宣称所有命令安全。
17|sudo reboot 仍 deny/critical_bash_command|test_auto_approve_allows_ask_but_not_hard_deny|SUPPORTED_EXACT|blocked的行为和规则精确断言。
17|accept_edits 同样不能绕硬拒绝|test_hard_deny_overrides_permission_mode|SUPPORTED_PARAPHRASE|同危险命令在accept_edits下deny_rule/critical_bash_command。
17|call-permission 经用户 deny|test_permission_evaluation_and_resolution_are_persisted|SUPPORTED_PARAPHRASE|批次write的call_id与user_confirmation deny，持久化resolved=user_denied。
17|permission_evaluated.source=default_fallback|test_permission_evaluation_and_resolution_are_persisted|SUPPORTED_EXACT|evaluated payload.source断言。
17|permission_resolved.resolution=user_denied|test_permission_evaluation_and_resolution_are_persisted|SUPPORTED_EXACT|resolved payload.resolution断言。
17|无确认入口 write 返回 status=error|test_unavailable_confirmation_returns_structured_denial|SUPPORTED_EXACT|该fixture没有user_confirmation，status=error。
17|failure.code=confirmation_unavailable|test_unavailable_confirmation_returns_structured_denial|SUPPORTED_EXACT|failure.code精确一致。
17|且文件不存在|test_unavailable_confirmation_returns_structured_denial|SUPPORTED_EXACT|a.txt.exists为False，不只判断模型回答。
'''


def read(p): return json.loads(p.read_text(encoding='utf-8'))
def digest(p): return sha256(p.read_bytes()).hexdigest()
def save(p,v): p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def snapshot(folder):
    return {str(p):digest(p) for p in folder.rglob('*') if p.is_file() and '__pycache__' not in str(p)}


def run_review(output):
    manifest=read(RUN/'execution_manifest.json')
    old=RUN.parent/'stage1-real-20261011T090900010334Z'
    before=dict(production=snapshot(REPO/'hexgent'),raw_run=snapshot(RUN),failed_run=snapshot(old),
        frozen={**manifest['protected_before'],**manifest['prep3_before']})
    f1=read(RUN/'F1-02/evidence.json'); l2=read(RUN/'L2/evidence.json')
    calls=[c for c in f1['calls'] if c['call_type']=='COMPACTION']
    main=next(c for c in f1['calls'] if c['call_type']=='MAIN')
    from hexgent.utils.token_estimator import estimate_request_tokens
    outcomes=[]
    for i,c in enumerate(calls):
        candidate=f1['actual_candidates'][i]
        summary=re.search(r'<summary>\s*(.*?)\s*</summary>',candidate,re.S).group(1).strip()
        req=deepcopy(main['request']);req['input'][0]['content']='以下是此前会话的压缩摘要：\n\n'+summary
        estimated=estimate_request_tokens(req['instructions'],req['input'],req['tools'])
        base=4038 if i==0 else 3550
        # 独立重算真实候选请求；没有实际执行任何候选或重新压缩。
        outcomes.append(dict(order=i+1,cycle=1 if i==0 else 2,attempt=c['logical_attempt'],
            provider_call_id=c['provider_call_id'],span_id=c['span_id'],request_hash=c['request_hash'],
            response_id=c['response']['id'],observation_sequences=[o['sequence'] for o in f1['observations'] if o.get('span_id')==c['span_id']],
            evidence_pointer=f'{RUN}/F1-02/evidence.json#/calls/{i}',provider_usage=c['usage'],
            provider_latency_ms=c['latency_ms'],candidate_summary=candidate,
            candidate_full_request_estimated_tokens=estimated,before_tokens=base,
            candidate_savings=base-estimated,candidate_savings_ratio=(base-estimated)/base,
            meaningful_savings=(base-estimated)>=2000 or (base-estimated)/base>=.1,
            accepted=i==0,rejection_reason=None if i==0 else 'INSUFFICIENT_COMPACTION_SAVINGS',
            decision_evidence='Transcript sequence 14' if i==0 else '独立重算+Production收益分支；最终失败Event只保存末次原因，逐候选拒绝Event未单独持久化'))
    assert [x['candidate_full_request_estimated_tokens'] for x in outcomes]==[3550,3550,3438]
    assert [x['meaningful_savings'] for x in outcomes]==[True,False,False]
    timeline=[dict(step=1,action='proactive触发',estimated_tokens=4038,threshold=3401,source='events[0]及run_configuration.context',
                   code='hexgent/context/context_compaction.py:165-181'),
              dict(step=2,action='周期1 attempt1',call=outcomes[0]),
              dict(step=3,action='partial接受与安全Checkpoint',transcript_sequences=[13,14,15],
                   active_message_count=2,round_boundaries=[1],partial_count=1,compaction_count=1,
                   checkpoint='cp-000001',code='context_compaction.py:280-313;agent_turn.py:362-372;agent_loop.py:112-135'),
              dict(step=4,action='Continue后再次proactive触发',estimated_tokens=3550,threshold=3401,source='events[4]、observation seq6',
                   prior_main_requests=0,reactive_compaction=False),
              dict(step=5,action='周期2 attempt1无收益拒绝',call=outcomes[1]),
              dict(step=6,action='周期2 attempt2收益不足拒绝',call=outcomes[2]),
              dict(step=7,action='软失败后MAIN使用已接受摘要',transcript_sequences=[16,17,18],
                   blocking_threshold=120000,final_estimated_input_tokens=3550,main_call=main['provider_call_id'],
                   main_span=main['span_id'],main_request=main['request'],
                   final_request_evidence='calls[3].request',finalization_status=f1['events'][-1]['result']['status'])]
    save(output/'f1_call_timeline.json',dict(original_run=str(RUN),timeline=timeline,candidates=outcomes))
    rejected=calls[1:]
    usage=lambda cs:dict(input_tokens=sum(c['usage']['input_tokens'] for c in cs),output_tokens=sum(c['usage']['output_tokens'] for c in cs),
                        total_tokens=sum(c['usage']['input_tokens']+c['usage']['output_tokens'] for c in cs))
    efficiency=dict(accepted_usage=usage(calls[:1]),rejected_usage=usage(rejected),all_usage=usage(calls),
        accepted_estimated_request_savings=488,accepted_estimated_request_reduction_ratio=488/4038,
        rejected_latency_ms=sum(c['latency_ms'] for c in rejected),
        amortization='AMORTIZED_BENEFIT_NOT_EVALUATED',net_agent_token_benefit='NOT_EVALUATED',
        attribution=[dict(category='CONFIGURATION_INDUCED',finding='冻结短样本阈值3401、recent100导致partial后3550仍超阈值',
                          evidence='execution_manifest.entries[0].compaction_parameters; events[1,4]'),
                     dict(category='EXPECTED_BY_CURRENT_POLICY',finding='proactive partial→Continue→重新检查；周期2尝试2次是现有有限Retry，不是reactive',
                          code='context_compaction.py:177-181,225-241,274-278;agent_turn.py:362-372;agent_loop.py:112-135'),
                     dict(category='SUSPECTED_POLICY_INEFFICIENCY',finding='无新增业务消息时重压同一摘要；第2次返回内容与已接受摘要相同，第3次仅112/3550=3.15%收益，被拒绝',
                          evidence='calls[1].request.input/actual_candidates[1,2]；2份候选Usage真实发生'),
                     dict(category='EXPECTED_BY_CURRENT_POLICY',finding='max_partial_compactions限制接受次数，不在LLM前阻止下一次partial检查；不是调用次数上限',
                          code='context_compaction.py:280-291'),
                     dict(category='INSUFFICIENT_EVIDENCE',finding='不能据该2例判断默认64K策略频繁浪费或整个Run净成本变差')],
        confirmed_dispatch_or_state_defect=False,eval_unexpected_duplicate_dispatch=False,
        contract_compliant=True,source_limits='3≤每样本4；SDK/Eval Retry=0；一次合法Production重试',
        suggestion='作为C2效率Finding保留，不为Stage2修改策略；若后续一般负载确认，可独立设计无新增消息的低收益再压缩抑制，不禁止硬阈值应急压缩。',
        known_observability_gap='Production Run Report usage仅Fake MAIN(1/2)，未汇总真实Compaction；不得用该字段替代本轮完整Eval Trace成本。C0已指出报告缺口。')
    save(output/'f1_efficiency_review.json',efficiency)
    comp=l2['calls'][0]; inp=comp['request']['input']; summary=l2['actual_candidates'][0]
    sources={}
    for i,m in enumerate(inp):
        if m.get('type')=='function_call_output':
            call=next(v for v in inp if v.get('type')=='function_call' and v['call_id']==m['call_id'])
            n=int(m['call_id'].split(':')[-1]);text=m['output'];lines=text.splitlines()
            start=next(j for j,line in enumerate(lines) if line.startswith('"""') or line.startswith('import unittest'))
            code='\n'.join(lines[start:]).split('\n合成审查台账',1)[0]
            tree=ast.parse(code)
            methods={v.name:(v.lineno+start,v.end_lineno+start) for v in ast.walk(tree) if isinstance(v,ast.FunctionDef)}
            sources[n]=dict(input_index=i,call_id=m['call_id'],path=json.loads(call['arguments'])['path'],text=text,
                            lines=lines,methods=methods,code_start=start,code=code)
    def evidence(n,methods):
        if n<0:return [dict(request_pointer=f'calls[0].request.input[{i}]',quote=json.dumps(m,ensure_ascii=False),
                             scope='原始user/assistant文本或调用参数') for i,m in enumerate(inp) if m.get('type')!='function_call_output'] + [
            dict(request_pointer=f'calls[0].request.input[{s["input_index"]}].output',
                 quote='\n'.join(s['lines'][:2]),scope='阅读证据限定头部') for s in sources.values()]
        s=sources[n];ranges=[]
        for name in methods.split(';'):
            if name=='*':ranges.append((0,len(s['lines'])-1))
            elif name.startswith('$'):
                j=next(j for j,line in enumerate(s['lines']) if line.startswith(name[1:]+' = '));ranges.append((j,j))
            elif name.startswith('@'):
                selected=[p for fn,p in s['methods'].items() if name[1:] in fn]
                assert selected, (n,name)
                ranges.extend(selected)
            else:
                assert name in s['methods'],(n,name)
                ranges.append(s['methods'][name])
        # helper函数同属已发送源码，恢复/校验相关链路完整保留，不使用后续MAIN。
        if n==7:ranges += [p for name,p in s['methods'].items() if name in ['pause','correct','resume','assert_only','record_block']]
        return [dict(source_call_id=s['call_id'],source_file=s['path'],request_input_index=s['input_index'],
                     pointer=f'calls[0].request.input[{s["input_index"]}].output',output_line_start=a+1,output_line_end=b+1,
                     quote='\n'.join(s['lines'][a:b+1])) for a,b in ranges]
    items=[]
    def add(n,q,methods,judgement,reason,scope='EXTENDED'):
        assert q in summary,q
        matches=[m.start() for m in re.finditer(re.escape(q),summary)]
        contexts=[summary[summary.rfind('\n',0,start)+1:summary.find('\n',start) if '\n' in summary[start:] else len(summary)] for start in matches]
        items.append(dict(assertion_id=f'L2-A{len(items)+1:03d}',summary_quote=q,summary_char_offsets=matches,summary_line_context=contexts,
                          scope=scope,classification=judgement,reason=reason,evidence=evidence(n,methods),
                          source_channel='ORIGINAL_COMPACTION_INPUT_ONLY'))
    for line in CLAIMS.strip().splitlines():
        n,q,methods,judgement,reason=line.split('|');add(int(n),q,methods,judgement,reason)
    # 自然语言任务状态与文件标识逐项核对完整原始请求。
    add(17,'继续逐文件只读审查测试','*','SUPPORTED_PARAPHRASE','末轮用户明确逐文件审查目标，未声称本轮真实完成审计。')
    add(-1,'区分已读实现、测试源码断言和尚未验证行为','*','SUPPORTED_PARAPHRASE','每轮用户要求区分实现/测试断言/未验证，后文明确未读实现；不是已验证Production。')
    add(17,'最新已读 tests/test_permission_pipeline.py','*','SUPPORTED_INFERENCE','原始摘要输入最后一轮call/output路径即该文件。')
    for n,s in sources.items():
        name=s['path'].split('/')[-1]
        add(n,name,'*','SUPPORTED_INFERENCE','原始输入存在该文件read_file及对应冻结正文；已阅读仅表示合成历史中可得，非真实Handler执行证据。')
    for q,reason in [
        ('只读，不修改文件、不执行测试','用户只读请求和每个返回头说明阅读证据，不是本轮测试通过。'),
        ('工具提供的是冻结源码阅读证据，不能称为本轮测试通过','18个output头均明确冻结源码阅读证据/非本轮测试结果。'),
        ('各用例输入、异常、状态及 Call ID 必须独立归属，不拼接成一次执行','每个output头明确不得拼接成一次结果。'),
        ('注释/测试名不能代替断言','证据方法论提醒，原输入有测试名/注释与assert区别；不是声称测试通过。'),
        ('导入实现不等于已审查实现','输入只有tests文件返回；import引用不是implementation正文。'),
        ('此前仅回复阅读确认','18条历史assistant只有固定已阅读确认。'),
        ('未形成详细审查报告','摘要输入assistant没有详细报告；限定该可见历史。'),
        ('未读对应生产实现','输入只有18个tests文件read，未提供独立实现正文；不外推Session其它未知历史。'),
        ('未运行任何用例','该可见历史没有测试执行工具；头部也说明未执行。'),
        ('以下均为测试源码断言而非执行结果','所有技术断言以源码断言为范围限定，没有改写成本轮PASS。'),
        ('源码断言','本段当前状态明确限定为源码断言。'),
        ('按用户下一文件继续阅读','后续计划符合逐文件只读任务，不声称已完成该步骤。'),
        ('需要结论时按源码断言/实现证据/未验证三层输出','任务要求区分证据层；计划不是已完成事实。'),
        ('对应实现规则顺序、异常完整覆盖、真实网络/服务安全、可靠并发及效果验收均不能凭上述测试宣称已验证','证据限制与原始输入只含冻结源码一致；不是未实现能力断言。'),
        ('这些是附加台账证据，不是测试断言或本轮检索实测','原输出合成审查台账明确独立于源码；未把检索台账变为真实运行。'),
        ('无执行错误或修改','可见历史只有阅读call、正常冻结output及确认，不是报告全Session未知状态。'),
        ('已阅读 tests/ 下','18个文件均有对应read_file及output，后续文件名逐项核对；限定合成阅读历史。'),
        ('保留合成台账当前有效项及来源','四条当前台账在对应冻结output末尾明确当前有效；两条废止另列，不混入当前集合。'),
    ]:add(-1,q,'*','SUPPORTED_INFERENCE',reason)
    ledger_facts=[(0,'目标查询 QUERY-WILLOW-804','目标查询=QUERY-WILLOW-804','REQUIRED'),
                  (7,'目标文档 DOC-W804-6','目标文档=DOC-W804-6','REQUIRED'),
                  (14,'完整响应 Dense Top30 未命中','完整响应 Dense Top30 未命中','REQUIRED'),
                  (16,'完整响应 Sparse 第7名、RRF 第11名','完整响应 Sparse 第7名且 RRF 第11名','REQUIRED'),
                  (1,'旧残缺响应排名=第2名','旧残缺响应排名=第2名','EXTENDED'),
                  (9,'旧结论=目标已进入Top5','旧结论=目标已进入Top5','EXTENDED')]
    for n,q,src,scope in ledger_facts:
        assert src in sources[n]['text']
        reason='台账当前实体/值一致；不使用Oracle答案或recent tail反证来源。' if scope=='REQUIRED' else '原输出明确该项已废止，摘要错误与恢复段明确不再生效；历史提及不是激活。'
        add(n,q,'*','SUPPORTED_PARAPHRASE',reason,scope)
    # 台账实体与出处映射也是独立断言；逐映射核对，不借用Oracle期望。
    for n,q in [(0,'test_agent_engine_config→目标查询'),(7,'test_context_record_resume→目标文档'),
                (14,'test_memory→完整响应 Dense'),(16,'test_no_session_request_budget→完整响应 Sparse')]:
        add(n,q,'*','SUPPORTED_PARAPHRASE','对应read_file路径及output尾部合成台账绑定一致。')
    add(-1,'已废止台账','*','SUPPORTED_PARAPHRASE','两项历史台账output都明确该项已废止，不再生效。')
    add(-1,'不再生效，不得用于当前结论','*','SUPPORTED_PARAPHRASE','原始两项台账均具有明确已废止/不再生效状态。')
    # 保存覆盖缺口用于人工检查，不把空白/连接词算独立事实。
    covered=[False]*len(summary)
    for item in items:
        for start in item['summary_char_offsets']:
            covered[start:start+len(item['summary_quote'])]=[True]*len(item['summary_quote'])
    gaps=[]
    for m in re.finditer(r'.+', ''.join(' ' if flag else c for c,flag in zip(summary,covered))):
        value=m.group().strip()
        if value:gaps.append(value)
    ext=[i for i in items if i['scope']=='EXTENDED'];counts=dict(Counter(i['classification'] for i in ext))
    save(output/'l2_assertion_inventory.json',dict(original_summary=summary,assertions=[{k:v for k,v in i.items() if k!='evidence'} for i in items],
        inventory_unit='每个独立测试事实、状态、标识符、台账映射或证据限制；同证据短语不同语境分别列项',
        scope='EXTENDED不包含4条REQUIRED；文件名逐个列项，不算测试场景数',uncovered_text_for_manual_check=gaps))
    save(output/'l2_assertion_review.json',dict(assertions=items,extended_assertion_count=len(ext),required_assertion_count=4,
        extended_classification_counts=counts,unsupported=sum(i['classification']=='UNSUPPORTED' for i in ext),
        contradicted=sum(i['classification']=='CONTRADICTED' for i in ext),uncertain=sum(i['classification']=='UNCERTAIN' for i in ext),
        stale_fact_activation=0,required_fact_recall='4/4',
        exhaustive_review_status='COMPLETE_WITH_UNCERTAIN_JUDGEMENTS',
        uncovered_text_review=dict(status='MANUALLY_REVIEWED',text=gaps,
            disposition='剩余为summary标签、章节/模块名、标点连接词及已拆解事实的连接表达；无未判定实质性断言。'),
        source_input_count=len(inp),source_scope='仅calls[0].request.input；不使用MAIN、tail、Plan、Pinned或Oracle作来源',
        safeguards='判定由人工阅读完整测试方法/断言/调用输入作出；脚本只定位与固化，不以关键词自动判正确。',
        rank8='摘要未声称第8名；源摘要输入不含该事实，它只在recent tail，不计入摘要成功。'))
    # Trace、账本、汇总三方核对，保留旧结果，不回写。
    all_calls=[c for ev in [f1,l2] for c in ev['calls']]
    real=[c for c in all_calls if c['call_type']=='COMPACTION'];fake=[c for c in all_calls if c['call_type']=='MAIN']
    assert len(real)==4 and len(fake)==2 and all(c['usage_complete'] for c in real)
    assert all(c['actual_network_dispatch'] for c in real) and not any(c['actual_network_dispatch'] for c in fake)
    totals=usage(real);assert totals==dict(input_tokens=40023,output_tokens=2534,total_tokens=42557)
    ledger=read(RUN/'budget_ledger.json');assert ledger['calls']==4 and ledger['halted_reason'] is None
    assert {c['provider_call_id'] for c in real}=={r['provider_call_id'] for r in ledger['reservations']}
    assert all(r['consumed'] for r in ledger['reservations'])
    top=read(RUN/'provider_trace.json')
    for fixture,ev in [('F1-02',f1),('L2',l2)]:
        assert next(t['calls'] for t in top['fixtures'] if t['fixture_id']==fixture)==ev['calls']
    for m in read(RUN/'token_metrics.json')['fixtures']:
        ev=f1 if m['fixture_id']=='F1-02' else l2
        req=ev['calls'][-1]['request']
        assert estimate_request_tokens(req['instructions'],req['input'],req['tools'])==m['post_compaction_full_request_estimated_input_tokens']
        assert usage([c for c in ev['calls'] if c['call_type']=='COMPACTION'])==dict(
            input_tokens=m['compaction_provider_input_tokens'],output_tokens=m['compaction_provider_output_tokens'],total_tokens=m['compaction_provider_total_tokens'])
    cross=dict(real_compaction_calls=4,fake_main_calls=2,usage_complete='4/4',actual_provider_usage=totals,
               sdk_retries=0,eval_retries=0,production_retry_attempts=1,production_cycles=3,
               errors=0,timeouts=0,accepted=2,rejected=2,budget_exceeded=False,
               confirmed_summary_conflicts=[],reported_latency_scope='Trace latency与Observation span latency覆盖边界不同，不静默替换',
               raw_execution_quality='QUALITY_REVIEW_PENDING',independent_review_gate='STAGE1_COMPLETED_WITH_QUALITY_FINDINGS',
               distinction='原始execution_result是执行/source Gate，不是语义Gate，后续result独立引用，没有改写原始结果',
               report_usage_gap='Production Run Report MAIN-only，不能替代Eval全调用Usage')
    save(output/'cross_check.json',cross)
    after=dict(production=snapshot(REPO/'hexgent'),raw_run=snapshot(RUN),failed_run=snapshot(old),
               frozen={p:digest(Path(p)) for p in before['frozen']})
    diff=subprocess.run(['git','diff','--check'],cwd=REPO,capture_output=True,text=True)
    checks={k+'_unchanged':before[k]==after[k] for k in before}
    checks.update(production_matches_stage1={p:digest(Path(p))==h for p,h in manifest['production_before'].items()},
                  production_hashes=after['production'],frozen_hashes=after['frozen'],
                  raw_run_hashes=after['raw_run'],failed_run_hashes=after['failed_run'],
                  git_diff_check_exit_code=diff.returncode,git_diff_check_output=diff.stdout+diff.stderr,
                  network_calls=0,review_script_sha256=digest(Path(__file__)))
    assert all(checks[k+'_unchanged'] for k in before) and all(checks['production_matches_stage1'].values()) and diff.returncode==0
    save(output/'hash_check.json',checks)
    save(output/'evidence_index.json',dict(original_run=str(RUN),original_failure_run=str(old),
        script=str(Path(__file__)),raw_artifacts=[dict(path=p,sha256=h) for p,h in before['raw_run'].items()],
        source_locations=['hexgent/context/context_compaction.py:165-332','hexgent/context/context_manager.py:154-232',
                          'hexgent/loop/agent_turn.py:344-380','hexgent/loop/agent_loop.py:112-135',
                          'eval/context_management/c2/stage1_live.py:36-48','eval/context_management/c2_dispatch_guard_repair/entry.py:27-74']))
    readiness=dict(gate='STAGE2_REVIEW_WITH_KNOWN_QUALITY_RISKS',stage2_approved=False,stage2_executed=False,
        recommend_continue_subject_to_human_approval=True,
        risks=['F1两次低收益再摘要增加1977 Provider Tokens和约33.14秒，单样本不能外推默认配置',
               'L2两条扩展断言源测试不足以证明强表述，已标UNCERTAIN，不能作为Production能力证据',
               'Run Report非全量摘要账单；正式成本只能使用所有attempt的Eval Provider Trace',
               '容量仍仅USER_ASSERTED_CAPACITY_128K，不是Provider全128K验证'],
        dispatch_defect=False,severe_source_conflict=False,original_required_fact_recall='9/9',
        extended_assertion_count=len(ext),classification_counts=counts,
        unsupported=0,contradicted=0,uncertain=2,stale_fact_activation=0,
        note='UNCERTAIN不是默认正确；审查穷尽而判断保留不确定性。不修改Fixture/Oracle/原摘要，不重采样。')
    assert len([x for x in ext if x['classification']=='UNCERTAIN'])==2
    save(output/'stage2_readiness.json',readiness)
    lines=['# C2 Stage 1 Post-Smoke Review','',
        'Gate：**STAGE2_REVIEW_WITH_KNOWN_QUALITY_RISKS**。建议在人工接受下列风险后审批Stage 2；本轮没有授权或执行Stage 2。','',
        f'原始 Run：`{RUN}`。原失败 Run：`{old}`。两者全部文件哈希未变。','',
        '## A. F1 调用重建','',
        '| 周期/Attempt | Provider Call ID | 完整请求估算候选 | Provider输入/输出 | 结论 |',
        '|---|---|---:|---:|---|']
    for c in outcomes:
        lines.append(f'| {c["cycle"]}/{c["attempt"]} | {c["provider_call_id"]} | {c["before_tokens"]} → {c["candidate_full_request_estimated_tokens"]} | {c["provider_usage"]["input_tokens"]}/{c["provider_usage"]["output_tokens"]} | '+('partial接受' if c['accepted'] else '收益不足拒绝')+' |')
    lines += ['',
        '首次4038≥冻结阈值3401，候选3550减少488（12.09%），满足相对收益≥10%但仍高于阈值，故partial。Transcript seq14提交、seq15创建cp-000001，活动消息为摘要+最新Query，计数1。',
        'Agent Turn返回Continue(context_compacted)，Loop重新检查同一状态；没有新用户/工具消息，也没有先调用MAIN。3550≥3401再次proactive触发（不是prompt_too_long应急）。第二候选与已接受摘要相同，独立重算3550，无收益；合法attempt2加严格提醒后候选3438只省112（3.15%），未达到绝对2000或相对10%，被拒绝。',
        '最终只保留第一摘要，Fake MAIN实际请求仍3550。第二周期失败发生在120000硬阈值以下，走failed而非blocked，再进入MAIN。未提交失败候选、未发现非预期Eval重复派发。',
        '源码：context_compaction.py:165–181、225–241、274–291、315–332；agent_turn.py:344–380；agent_loop.py:112–135；context_manager.py:154–232。每步Span/Call/Event/Checkpoint引用见f1_call_timeline.json。逐候选拒绝未各自落Event，前一次拒绝依据原候选、后续attempt和同一Production分支独立重算，未伪造拒绝事件。','',
        '## B. 三种成本与效率归因','',
        '- 已实现：单次完整请求估算减少488，不计被拒候选3438为收益。',
        '- 接受调用Usage：1127输入+353输出=1480；拒绝候选合计1363输入+614输出=1977；F1所有调用2490输入+967输出=3457。',
        '- 拒绝候选耗时约33.14秒（Eval调用Trace口径）；完整F1 Run约83.63秒。Observation Span和Trace计时边界不同，保留各自原值，不混用。',
        '- AMORTIZED_BENEFIT_NOT_EVALUATED：只有一次Fake后续主请求，没有真实后续请求复用对照；估算减少488与Provider3457不能混单位相减。',
        '- CONFIGURATION_INDUCED + EXPECTED_BY_CURRENT_POLICY：短样本3401/近期100触发，partial后重新检查和有限重试符合现有契约。3次≤每例4次，SDK/Eval Retry均0，Production Retry1次。',
        '- SUSPECTED_POLICY_INEFFICIENCY：无新增消息重压摘要，第二候选重复、第三候选仍收益不足。max_partial_compactions限制接受次数而非派发次数，检查位于生成候选之后。',
        '- 未确认派发/上下文状态逻辑缺陷。无需为Stage 2修改Production；如更多样本确认低收益反复压缩，再另行审批抑制无新增消息的低收益再检查，不改变硬阈值安全兜底。',
        '- 已知观测缺口仍存在：F1 Run Report usage仅Fake MAIN的1/2；Checkpoint LoopState记1128/355，仅已接受摘要+Fake MAIN，未计第二周期失败摘要成本。C0报告第210行已列出该边界，不是本次新发现的派发Bug；Eval Trace完整统计所有4次真实摘要，Stage 2必须继续使用该口径。','',
        '## C. L2 逐断言审查','',
        f'完整摘要拆成{len(items)}条记录，其中4条为原REQUIRED、{len(ext)}条为扩展断言。扩展不是场景数量；18个源码文件名、独立值/状态/证据限制各自列项。',
        f'扩展分类：{json.dumps(counts,ensure_ascii=False)}。UNSUPPORTED=0、CONTRADICTED=0、UNCERTAIN=2；未错误激活过期事实。',
        '每条保留摘要原句/上下文、原请求input序号、call_id、冻结源码output行号、完整测试方法/助手函数证据及人工语义判定理由。没有以关键词自动判正确；没有使用Oracle预期、recent tail、MAIN、Plan或Pinned反证来源。',
        '两个UNCERTAIN：',
        '1. 固定Skill不截断：test_memory.py仅assertIn一段规则、warning、Token>1，没有全文相等/包含断言；即使只保留一个重复片段也可能通过。注释不能补作全文不截断的测试证据。',
        '2. 信号入口不等待阻塞回调：回调wait(timeout=1)，随后仅检查interrupted和callback_started；假设入口同步等待1秒后返回也可能通过。未严格验证非阻塞时序，不据此推导生产实现必然阻塞。',
        '其余技术结论均以测试源码断言为范围，不改写为本轮测试PASS。Stage 1历史是冻结合成阅读序列，不是本轮真实执行这些测试。旧第2名与已进入Top5均明确废止，未当作当前事实。摘要没有声称最终第8名；该事实仅在recent tail，未计入摘要分母。',
        '审查完成包含不确定判定，并非所有断言已证明正确。库存未覆盖字符已人工复核为章节/标签/连接词，没有实质性事实遗留；证据见l2_assertion_inventory.json和l2_assertion_review.json。','',
        '## D. 交叉核对与完整性','',
        '- 总真实摘要4，Fake MAIN2；Usage4/4完整，真实输入40023、输出2534。三个独立原始Trace副本、账本Call ID与汇总Token交叉一致，无严重汇总冲突。',
        '- 摘要接受2、收益不足候选拒绝2；原Required Fact Recall仍F1 5/5、L2 4/4，合计9/9，不扩大原Oracle分母。',
        '- 原execution_result的QUALITY_REVIEW_PENDING是执行层占位，后续独立result/quality_review才是质量结果，未修改两者。',
        '- Production、C1、C2-Prep/2/3、最终Stage1及旧失败Run哈希未变，git diff --check通过。本輪没有重新执行测试；原90/90仅引用原Run test_results.txt，不冒充本轮执行。',
        '- 冻结Prep3未存在token_budget_policy.md，不创建同名替代；预算实际由smoke_manifest.json、Prep3/budget.py的Token-only策略及Prep2/budget_policy.md的历史规则解释，最终批准的Stage1合同优先。','',
        '## E. Stage 2 建议','',
        '建议人工有条件批准继续：派发证据完整，无严重来源冲突；两条不确定表述不能用作Production能力宣传，F1额外成本须保持逐attempt记录。不得调阈值、摘要Prompt、Fixture或Oracle以制造通过。Stage 2仍NOT_APPROVED / NOT_RUN。','',
        '新增审查脚本无SDK/Provider调用入口，并禁socket连接；本轮真实调用次数为0。原始Artifact未修改，没有commit/tag。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(dict(output=str(output),extended_count=len(ext),counts=counts,uncovered=gaps,
                          efficiency=efficiency['rejected_usage'],hashes_unchanged=True),ensure_ascii=False))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);args=parser.parse_args()
    output=args.output or REPO/'eval/context_management/c2/reviews'/('stage1-post-smoke-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True,exist_ok=False)
    # 全过程明确禁网；无凭证加载、无SDK或模型入口。
    def denied(*args,**kwargs):raise AssertionError('离线审查禁止网络')
    socket.socket.connect=denied;socket.socket.connect_ex=denied;socket.create_connection=denied
    run_review(output)


if __name__=='__main__':main()
