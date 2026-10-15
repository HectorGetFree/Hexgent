"""B2 固定 Full 配置、只读指标与无权重汇总；不是任务调度系统。"""
from collections import Counter
from dataclasses import asdict,replace
from datetime import datetime
import json
from pathlib import Path
from statistics import mean,median
from hexgent.config import HexgentConfig
from hexgent.loop.limits import LoopLimits


def full_config(budget):
    config=HexgentConfig(loop=LoopLimits(max_model_rounds=40,max_tool_calls=budget['max_tool_calls'],max_total_tokens=budget['max_provider_tokens'],max_elapsed_seconds=budget['max_wall_time_seconds'],max_output_tokens_per_round=4096))
    return replace(config,context_extraction=replace(config.context_extraction,enabled=False),context_retrieval=replace(config.context_retrieval,enabled=False),tool_parallel=replace(config.tool_parallel,enabled=False))


def raw_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def metrics(root):
    usage=json.loads((root/'provider-usage.json').read_text()) if (root/'provider-usage.json').exists() else {'records':[]}
    reports=[json.loads(p.read_text()) for p in sorted(root.glob('sessions/*/reports/run-*.json'))]
    transcripts=[r for p in sorted(root.glob('sessions/*/transcript.jsonl')) for r in raw_rows(p)]
    observations=[r for p in sorted(root.glob('sessions/*/observation.jsonl')) for r in raw_rows(p)]
    groups={}
    records=[r for r in usage['records'] if r['kind']=='main']
    # 辅助调用按原生Provider span计一次，避免与Compaction成本账本重复相加。
    started={r['span_id']:r for r in observations if r['event']=='provider_started'}
    for r in observations:
        if r['event']!='provider_finished': continue
        kind=started.get(r.get('span_id'),{}).get('request_type',r.get('request_type'))
        if kind and kind!='MAIN_AGENT':
            records.append({'kind':kind,'usage':{'input_tokens':r.get('input_tokens'),'output_tokens':r.get('output_tokens')}})
    for record in records:
        group=groups.setdefault(record['kind'],{'requests':0,'input_tokens':0,'output_tokens':0,'cached_tokens':0,'reasoning_tokens':0,'missing_usage_records':0,'cached_missing_records':0,'reasoning_missing_records':0})
        group['requests']+=1;u=record.get('usage') or {}
        for field in ['input_tokens','output_tokens']:
            value=u.get(field)
            if type(value) is int:group[field]+=value
        if any(u.get(f) is None for f in ['input_tokens','output_tokens']):group['missing_usage_records']+=1
        for field,nested in [('cached_tokens','input_tokens_details'),('reasoning_tokens','output_tokens_details')]:
            value=(u.get(nested) or {}).get(field)
            if type(value) is int:group[field]+=value
            else:group[field.split('_')[0]+'_missing_records']+=1
    for g in groups.values():g['total_tokens']=g['input_tokens']+g['output_tokens']
    complete=bool(records) and not any(g['missing_usage_records'] for g in groups.values())
    completed=[r for r in transcripts if r['type']=='tool_execution_finished']
    starts=[r for r in transcripts if r['type']=='tool_execution_started']
    dispatch=[r for r in transcripts if r['type']=='tool_execution_dispatched']
    compactions=[r for r in transcripts if r['type']=='context_compaction_applied']
    retries=[r for r in transcripts if r['type']=='tool_execution_retried']
    timestamps=[datetime.fromisoformat(r[f]) for r in reports for f in ['started_at','finished_at']]
    return {'provider_groups':groups,'known_provider_tokens':sum(g['total_tokens'] for g in groups.values()),'recorded_usage_complete':complete,
        'usage_coverage':'PARTIAL: unreported failed/SDK retries unknown; Workspace embedding usage unavailable',
        'sessions':len(list(root.glob('sessions/*/transcript.jsonl'))),'agent_turns':sum(r['type']=='model_response_completed' for r in transcripts),
        'tool_calls':len(starts),'tool_dispatches':len(dispatch),'tool_errors':sum(r['payload']['status'] in {'error','invalid'} for r in completed),
        'tool_denials':sum(r['payload']['status']=='denied' for r in completed),'automatic_retry_events':sum(max(0,r['payload'].get('attempt_count',1)-1) for r in completed),
        'handler_attempts':sum(r['event']=='handler_started' for r in observations),
        'agent_run_seconds_sum':sum(r['elapsed_seconds'] for r in reports) if reports else None,
        'agent_span_seconds':(max(timestamps)-min(timestamps)).total_seconds() if timestamps else None,
        'compactions':len(compactions),'snapshot_events':sum(bool(r['payload'].get('key_fact_snapshot')) for r in compactions),
        'snapshot_provider_calls':sum(r['event']=='provider_started' and r.get('request_type')=='FACT_EXTRACTION' for r in observations),
        'memory_writes':sum(r['payload']['tool_name']=='remember' and r['payload']['status']=='success' for r in completed),
        'memory_recall_requests':sum(r['event']=='memory_injected' and bool(r.get('memories')) for r in observations),
        'memory_recall_items':sum(len(r.get('memories',[])) for r in observations if r['event']=='memory_injected'),
        'checkpoints':sum(r['type']=='checkpoint_committed' for r in transcripts),'restores':sum(r['type']=='session_recovered' for r in transcripts),
        'native_run_configurations':[r for r in observations if r['event']=='run_configuration'],
        'provider_failures':[r for r in observations if r['event']=='provider_finished' and r.get('status') not in {'SUCCEEDED','SUCCEEDED_CANDIDATE_REJECTED'}],
        'tool_statuses':dict(Counter(r['payload']['status'] for r in completed)),
        'agent_output_scope':'final natural-language answer not used for scoring'}


def aggregate(records,inventory):
    result=[]
    for case in inventory:
        rows=[r for r in records if r['case_id']==case['case_id']]
        valid=[r for r in rows if r['valid_task_sample']]
        counts=Counter(r['task_status'] for r in rows)
        def stats(field):
            values=[r['metrics'].get(field) for r in valid if r['metrics'].get(field) is not None]
            return {'mean':mean(values),'median':median(values),'min':min(values),'max':max(values)} if values else None
        result.append({'case_id':case['case_id'],'task_id':case['task_id'],'eligible':case['eligible'],'attempts':len(rows),'valid_trials':len(valid),
            'pass':sum(r['task_status']=='PASS' for r in valid),'fail':sum(r['task_status']=='FAIL' for r in valid),
            'invalid':counts['INVALID'],'inconclusive':counts['INCONCLUSIVE'],'excluded_attempts':len(rows)-len(valid),
            'pass_rate_numerator':sum(r['task_status']=='PASS' for r in valid),'pass_rate_denominator':len(valid),
            'pass_rate':sum(r['task_status']=='PASS' for r in valid)/len(valid) if valid else None,
            'known_provider_tokens':stats('known_provider_tokens'),'agent_run_seconds_sum':stats('agent_run_seconds_sum'),'tool_calls':stats('tool_calls'),
            'failures':[{'trial_id':r['trial_id'],'task_status':r['task_status'],'failure_category':r.get('failure_category'),'reason':r.get('reason')} for r in rows if r['task_status']!='PASS']})
    return result
