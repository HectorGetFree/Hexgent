"""Checkpoint之后的Run预算/控制投影重放；不执行工具或重新请求模型。"""
from dataclasses import replace
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from ..loop.loop_state import RunStateSnapshot
from ..skill import SkillDefinition


COUNTERS = ('model_rounds', 'tool_calls', 'input_tokens', 'output_tokens', 'elapsed_seconds')
STATEFUL = {'context_user_query_appended', 'context_messages_appended',
            'context_compaction_applied', 'model_response_completed', 'agent_run_finished'}


def supports_run_replay(checkpoint: Any, records: list[dict]) -> bool:
    through = checkpoint.transcript_sequence if checkpoint is not None else 0
    starts = [r for r in records if r['type'] == 'agent_run_started']
    return bool(starts and 'run_state_version' in starts[-1]['payload']) or any(
        r['sequence'] > through and 'run_state_version' in r['payload'] for r in records)


def replay_run_state(checkpoint: Any, records: list[dict]) -> RunStateSnapshot | None:
    """完整状态发布作为锚点，之后工具账本按call_id计一次；未知成本不猜测。"""
    try:
        return _replay(checkpoint, records)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _replay(checkpoint, records):
    through = checkpoint.transcript_sequence if checkpoint is not None else 0
    state = (RunStateSnapshot.from_dict(checkpoint.run_state)
             if checkpoint is not None and checkpoint.run_status in {'active','continuable'}
             and isinstance(checkpoint.run_state,dict) else None)
    active = next((r['payload']['run_id'] for r in reversed(records)
                   if r['sequence'] <= through and r['type']=='agent_run_started'), None)
    valid = state is not None
    paused = checkpoint is not None and checkpoint.run_status=="continuable"
    anchor_time = None
    last_time = None
    calls = {};dispatched = set();finished = set();charged = set()
    for record in records:
        if record['sequence'] <= through:
            if record['sequence'] == through:anchor_time = record['created_at']
            continue
        kind = record['type'];p = record['payload'];last_time = record['created_at']
        if kind == 'agent_run_started':
            active = p['run_id']
            if not isinstance(active,str) or not active:raise ValueError('Run身份无效')
            valid = False;paused=False
        publishes = kind in {'agent_run_started','context_messages_appended','run_state_committed','context_user_query_appended'} and 'run_state_version' in p
        compaction = kind == 'context_compaction_applied' and 'resume_state' in p
        if publishes or compaction:
            if publishes and (type(p['run_state_version']) is not int or p['run_state_version'] != 1 or p['run_id'] != active or not active):
                raise ValueError('Run状态版本/身份不匹配')
            new = RunStateSnapshot.from_dict(p['run_state'] if publishes else p['resume_state'])
            if state is not None and kind != 'agent_run_started':
                if any(getattr(new,k) < getattr(state,k)+(len(charged) if k=='tool_calls' else 0) for k in COUNTERS):
                    raise ValueError('Run资源计数回退')
            status=p.get('run_status','active')
            if status not in {'active','continuable','terminal'}:raise ValueError('未知Run状态')
            state = new;valid = status!='terminal';paused=status=='continuable';anchor_time = last_time
            calls = {m['call_id']:m.get('name') for m in p.get('messages',[])
                     if isinstance(m,dict) and m.get('type')=='function_call'}
            if kind=='context_messages_appended' and p.get('source')=='model_response' and not calls:
                valid=False  # 无工具响应必须等Plan退出/终态控制发布，不擅自再启动一轮模型。
            dispatched=set();finished=set();charged=set()
            continue
        if kind == 'run_state_invalidated':
            if p['run_id'] != active:raise ValueError('跨Run失效记录')
            valid = False
        elif kind in {'tool_execution_started','tool_execution_dispatched','tool_execution_finished'}:
            cid = p['call_id']
            if cid not in calls or calls[cid] != p['tool_name']:
                valid = False;continue
            if kind == 'tool_execution_dispatched':
                if cid in dispatched:raise ValueError('重复工具派发')
                dispatched.add(cid);charged.add(cid)
            elif kind == 'tool_execution_finished':
                if cid in finished:raise ValueError('重复工具结果')
                finished.add(cid)
                status=p['status']
                failure=p.get('failure') or {}
                never_admitted=(status=='error' and p.get('attempt_count')==0
                    and failure.get('phase')=='scheduling' and failure.get('side_effect_state')=='not_started'
                    and failure.get('code') in {'steering_interrupted','execution_interrupted','tool_timeout'})
                if never_admitted:charged.discard(cid)
                elif status in {'success','error','deduplicated'}:charged.add(cid)
                elif status not in {'denied','invalid','skipped','interrupted'}:raise ValueError('未知工具状态')
                if status=='interrupted':charged.discard(cid)  # 已确认中断按原Kernel计数；未知派发仍保留消耗。
                if p['tool_name']=='activate_skill' and status=='success':
                    state=replace(state,active_skill=SkillDefinition.from_dict(p['data']['skill']))
        elif kind == 'agent_run_finished':
            # 终态Checkpoint或新的完整控制发布才能证明原Run仍可续跑。
            valid = valid and paused and p.get('run_id')==active and p.get('status') in {'interrupted','incomplete'}
        elif kind in STATEFUL:
            # Restore生成的保守工具结果不消耗模型/工具预算；重复Restore仍幂等。
            if not (kind=='context_messages_appended' and p.get('source')=='session_recovery'):
                valid = False
    if state is None or not valid:return None
    elapsed = state.elapsed_seconds
    if anchor_time is not None and last_time is not None:
        end=datetime.fromisoformat(last_time)
        # 未正常结束的active Run按墙钟保守计费，重启等待时间不能变成新预算。
        if not paused:
            end=max(end,datetime.now(timezone.utc))
        delta = (end-datetime.fromisoformat(anchor_time)).total_seconds() if not paused else 0.0
        if not isfinite(delta) or delta<0:raise ValueError('事件时间倒退')
        elapsed += delta
    return replace(state,tool_calls=state.tool_calls+len(charged),elapsed_seconds=elapsed,
                   transition='tool_results_ready' if calls else state.transition)
