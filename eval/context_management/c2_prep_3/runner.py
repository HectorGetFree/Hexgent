"""七例最终Dry Run；入口仅FakeModel，不包含真实Provider创建逻辑。"""
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json

from .prepare import ROOT, read_json, digest, snapshot, freeze
from .budget import TokenBudget, TokenPolicy, TokenTrace, require_stage_approval
from eval.context_management.c2_prep_2.runner import seed
from eval.context_management.c2_prep_2.control import reconcile
from eval.context_management.c2_prep.prepare import StreamRecorder
from eval.context_management.c1_independent import Gate, plain, save, hashes, response
from hexgent.agent_engine import AgentEngine
from hexgent.config import validate_config
from hexgent.utils.token_estimator import estimate_request_tokens


def policy(manifest,stage=None):
    entries=[e for e in manifest['fixtures'] if stage is None or e['stage']==stage]
    # 各阶段额度独立上限，同一阶段的Fixture共用一个锁与账本。
    return TokenPolicy(sum(e['max_compaction_calls'] for e in entries),
        sum(e['max_compaction_calls']*e['max_reserved_input_per_call'] for e in entries),
        sum(e['max_compaction_calls']*e['max_output_tokens_per_call'] for e in entries),
        {e['fixture_id']:e for e in entries},manifest['policy']['input_margin_ratio'])


def validate_capacity(manifest,confirmation):
    if not confirmation.get('confirmed') or confirmation['provider_host']!=manifest['provider']['host'] or confirmation['model_id']!=manifest['provider']['model']:
        raise RuntimeError('CAPACITY_NOT_CONFIRMED')
    for e in manifest['fixtures']:
        need=e['max_reserved_input_per_call']+e['max_output_tokens_per_call']+e['compaction_parameters']['safety_margin_tokens']
        if need>confirmation['minimum_confirmed_context_window_tokens']:
            raise RuntimeError('CAPACITY_MARGIN_INSUFFICIENT')


def load_rounds(entry):
    path=Path(entry['fixture_path'])
    if digest(path)!=entry['fixture_hash'] or digest(Path(entry['oracle_v2_path']))!=entry['oracle_v2_hash']:
        raise RuntimeError('FROZEN_INPUT_HASH_CHANGED')
    fixture=read_json(path)
    if entry['fixture_id'].startswith('L'):
        return fixture['rounds'],fixture['final_query']
    rounds=[]
    for index,m in enumerate(fixture['messages']):
        if m['role']=='user': rounds.append(dict(query=m['text'],messages=plain(response('已收到更正').output)))
        else:
            rounds.append(dict(query='核对资料来源 '+m['id'],messages=[
                dict(type='function_call',call_id=m['id'],name='read_file',arguments=json.dumps({'path':f'source-{index}.md'})),
                dict(type='function_call_output',call_id=m['id'],output=m['text'])]+plain(response('已核对').output)))
    return rounds,'继续核对当前有效事实与待办状态，不执行业务变更。'


def execute(entry,output,budget,summary_override=None):
    identifier=entry['fixture_id']; directory=output/identifier; directory.mkdir(parents=True)
    rounds,query=load_rounds(entry)
    config=Gate(output).config()
    config=validate_config(replace(config,context=replace(config.context,**entry['compaction_parameters'])))
    engine=AgentEngine(config,directory/'workspace'); seed(engine,rounds)
    oracle=read_json(Path(entry['oracle_v2_path']))
    for f in oracle['facts']:
        if f['summary_responsibility']=='EXTERNALLY_PROTECTED':
            engine.manage_pinned_constraint('pin',f['fact_id'],text=f['quote'])
    summary='<summary>当前目标：继续资料交接。重要发现：\n'+'\n'.join(f['quote'] for f in oracle['facts'] if f['summary_responsibility']=='SUMMARY_REQUIRED')+'\n剩余工作：整理当前状态。</summary>'
    evidence={'requests':[]}
    fake=StreamRecorder(evidence,summary=summary_override or [summary])
    native=SimpleNamespace(responses=fake,max_retries=0,base_url='offline://fake')
    trace=TokenTrace(native,directory/'provider-trace.json',budget.bind(identifier),eval_run_id=output.name,
        fixture_id=identifier,session=engine.session,main_fake=fake)
    # 所有实际调用为Fake；模型ID按合同记录，不构造SDK或发出网络请求。
    with patch('hexgent.loop.agent_turn.get_model',return_value='gpt-6.1-sol'):
        events=list(engine.run(query,client=trace))
    obs=[json.loads(line) for line in (engine.session.store.session_dir/'observation.jsonl').read_text().splitlines()]
    attribution=reconcile(trace,obs)
    calls=trace.state['calls']; comp=[c for c in calls if c['call_type']=='COMPACTION']; main=[c for c in calls if c['call_type']=='MAIN']
    tail=[m for m in main[0]['request']['input'] if not (m.get('role')=='developer' and str(m.get('content','')).startswith('以下是此前会话的压缩摘要'))] if main else []
    source_checks=[]
    for f in oracle['facts']:
        channel=f['summary_responsibility']
        in_prefix=bool(comp) and f['quote'] in json.dumps(comp[0]['request']['input'],ensure_ascii=False)
        in_tail=f['quote'] in json.dumps(tail,ensure_ascii=False)
        in_pinned=bool(main) and f['quote'] in main[0]['request']['instructions']
        valid=(channel=='OBSOLETE' or (channel=='SUMMARY_REQUIRED' and in_prefix and not in_tail) or
               (channel=='RECENT_TAIL_PRESERVED' and in_tail and not in_prefix) or
               (channel=='EXTERNALLY_PROTECTED' and in_pinned))
        source_checks.append(dict(fact_id=f['fact_id'],responsibility=channel,in_summary_input=in_prefix,in_recent_tail=in_tail,
            in_pinned=in_pinned,pass_=valid))
    result=dict(fixture_id=identifier,status='PASS' if attribution['pass_'] and comp and main and
        engine.context_manager.compaction_count and all(c['pass_'] for c in source_checks) else 'FAIL',
        compaction_calls=len(comp),main_fake_calls=len(main),attribution=attribution,source_checks=source_checks,
        summary_recall_denominator=sum(f['summary_responsibility']=='SUMMARY_REQUIRED' for f in oracle['facts']),
        summary_quality='NOT_EVALUATED_FAKE_RESPONSE',accepted=engine.context_manager.compaction_count,
        provider_usage_kind='FAKE_FIXTURE_PROTOCOL_USAGE_NOT_REAL_PROVIDER_COST')
    save(directory/'evidence.json',dict(result=result,events=plain(events),transcript=engine.session.store.read_transcript(),
        observations=obs,calls=calls,context=plain(engine.context_manager),candidate=summary,reservations=budget.reservations))
    return result


def main():
    manifest=read_json(ROOT/'smoke_manifest.json'); confirm=read_json(ROOT/'capacity_confirmation.json')
    validate_capacity(manifest,confirm)
    before,protected=hashes(),snapshot()
    output=ROOT/'runs'/('final-dry-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')); output.mkdir(parents=True)
    results=[]; budgets=[]; review=None
    for stage in (1,2):
        # 这里只给离线Stage2发放标为模拟的许可；绝不是实际Stage1人工审批。
        approval=None if stage==1 else {'approved':True,'stage1_run_id':review['run_id'],'evidence_type':'OFFLINE_APPROVAL_GATE_TEST'}
        require_stage_approval(stage,approval,review,dry_run=True)
        budget=TokenBudget(policy(manifest,stage))
        stage_results=[execute(e,output,budget) for e in manifest['fixtures'] if e['stage']==stage]
        results.extend(stage_results); budgets.append(dict(stage=stage,calls=budget.calls,reserved_input=budget.inputs,
            reserved_output=budget.outputs,max_calls=budget.policy.max_compaction_calls,reservations=budget.reservations))
        review=dict(run_id=output.name,audit_valid=all(r['status']=='PASS' for r in stage_results),evidence_type='FAKE_ONLY')
    final=dict(results=results,stage_budgets=budgets,manifest_hash=digest(ROOT/'smoke_manifest.json'),
        capacity_confirmation_hash=digest(ROOT/'capacity_confirmation.json'),output=str(output),paid_calls=0,
        production_unchanged=before==hashes(),protected_assets_unchanged=protected==snapshot(),
        production_hashes=before,evidence_type='DETERMINISTIC_DRY_RUN_NOT_REAL_C2',
        gate='READY_FOR_C2_SMOKE_REVIEW' if all(r['status']=='PASS' for r in results) and before==hashes() and protected==snapshot() else 'BLOCKED_FOR_C2')
    save(output/'result.json',final)
    if not (ROOT/'final_dry_run.json').exists():
        freeze(ROOT/'final_dry_run.json',final)
    return final


if __name__=='__main__':main()
