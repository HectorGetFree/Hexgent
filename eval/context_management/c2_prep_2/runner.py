"""C2-Prep-2 全离线 Dry Run，真实 Agent Loop 与默认长历史压缩配置。"""
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import replace, asdict
from types import SimpleNamespace
from unittest.mock import patch
import json
import hashlib

from .control import Budget, Policy, Trace, reconcile
from .fixtures import build
from eval.context_management.c2_prep.prepare import assets, StreamRecorder
from eval.context_management.c2_prep.catalog import CASES
from eval.context_management.c1_independent import Gate, plain, save, hashes, response
from hexgent.agent_engine import AgentEngine
from hexgent.config import validate_config
from hexgent.context.context_builder import build_context_details
from hexgent.session.pinned_constraints import active_constraints
from hexgent.loop.agent_turn import _available_tools_for_state
from hexgent.loop.loop_state import LoopState
from hexgent.utils.token_estimator import estimate_request_tokens

ROOT = Path(__file__).resolve().parent


def protected_hashes():
    repo = ROOT.parents[2]
    dirs = [ROOT.parent/'c2_prep', ROOT.parent/'runs'/'c1-independent-20261011T065644251729Z']
    return {str(p.relative_to(repo)):hashlib.sha256(p.read_bytes()).hexdigest()
            for directory in dirs for p in directory.rglob('*') if p.is_file() and '__pycache__' not in str(p)}


def fake_policy():
    return Policy(80, 128000, 8000, 10240000, 640000, '1000', '2', '10', 'FAKE_TEST_TARIFF_NOT_PROVIDER_PRICE', True, True)


def seed(engine, rounds):
    for r in rounds:
        query = engine.context_manager.append_user_query(r['query'])
        engine.session.record_user_query(query)
        engine.context_manager.append_messages(r['messages'], completes_round=True)
        engine.session.record_context_messages(r['messages'], completes_round=True, source='synthetic_fixture')


def run_fixture(identifier, rounds, facts, output, config, latest, shared_budget=None):
    directory = output/identifier
    directory.mkdir(parents=True)
    engine = AgentEngine(config, directory/'workspace')
    seed(engine, rounds)
    for f in facts:
        if f['channel']=='EXTERNALLY_PROTECTED':
            engine.manage_pinned_constraint('pin', f['fact_id'], text=f['quote'])
    evidence = {'requests':[]}
    summary = '<summary>当前目标：继续交接。重要发现：\n'+'\n'.join(f['quote'] for f in facts if f['channel']=='SUMMARY_REQUIRED')+'\n剩余工作：完成当前有效结论交接。</summary>'
    delegate = SimpleNamespace(responses=StreamRecorder(evidence, summary=[summary]), base_url='offline://fake', max_retries=0)
    # 输入估算只作 Fake 预算测试，真实调用必须另行提供已确认的 Token 上界。
    budget = shared_budget or Budget(fake_policy(), lambda r:estimate_request_tokens(r.get('instructions',''), r.get('input',[]), r.get('tools',[])))
    trace = Trace(delegate, directory/'provider-trace.json', budget, eval_run_id=output.name,
                  fixture_id=identifier, session=engine.session)
    instructions = build_context_details(engine.tool_context, pinned_constraints=active_constraints(engine.session.store)).instructions
    request_tools = _available_tools_for_state(LoopState(engine.context_manager,engine.tool_context))
    if identifier.startswith('F'):
        base = estimate_request_tokens(instructions, [{'role':'user','content':latest}], request_tools)
        engine.config = validate_config(replace(engine.config, context=replace(engine.config.context,
            proactive_threshold_tokens=base+250, recent_context_tokens=100, reactive_recent_context_tokens=50)))
    pre = estimate_request_tokens(instructions,engine.context_manager.materialize_messages(engine.context_manager.active_messages),request_tools)
    save(directory/'frozen-state.json',dict(config=asdict(engine.config),initial_estimated_input=pre, facts=facts))
    with patch('hexgent.loop.agent_turn.get_model', return_value='FAKE_ONLY'):
        events = list(engine.run(latest, client=trace))
    observations = [json.loads(line) for line in (engine.session.store.session_dir/'observation.jsonl').read_text().splitlines()]
    attribution = reconcile(trace,observations)
    calls = trace.state['calls']
    main = [c for c in calls if c['call_type']=='MAIN']
    comp = [c for c in calls if c['call_type']=='COMPACTION']
    external = bool(main) and all(f['quote'] in main[0]['request']['instructions'] for f in facts if f['channel']=='EXTERNALLY_PROTECTED')
    accepted = engine.context_manager.compaction_count
    default = engine.config.context.proactive_threshold_tokens==64000
    ok = bool(comp and main and accepted and external and attribution['pass_'])
    save(directory/'evidence.json',dict(events=plain(events), transcript=engine.session.store.read_transcript(),
        observations=observations, attribution=attribution, calls=calls, context=plain(engine.context_manager),
        reservations=budget.reservations, external_protection=external, fake_quality='NOT_EVALUATED',
        compaction_default_threshold=default, summary_candidate=summary))
    return dict(id=identifier,status='PASS' if ok else 'FAIL',compaction_calls=len(comp),main_calls=len(main),
                accepted=accepted, attribution=attribution,default_threshold=default,
                initial_estimated_input=pre,external_protection=external)


def main():
    before, protected = hashes(), protected_hashes()
    manifest = build()
    output = ROOT/'runs'/('dry-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    cfg = Gate(output).config()
    shared_budget = Budget(fake_policy(),lambda r:estimate_request_tokens(r.get('instructions',''),r.get('input',[]),r.get('tools',[])))
    results = []
    for case in CASES:
        fixture,facts = assets(case)
        rounds=[]
        for index,m in enumerate(fixture['messages']):
            if m['role']=='user':
                rounds.append(dict(query=m['text'],messages=plain(response('已收到更正').output)))
            else:
                rounds.append(dict(query='核对资料来源 '+m['id'],messages=[
                    dict(type='function_call',call_id=m['id'],name='read_file',arguments=json.dumps({'path':f'source-{index}.md'})),
                    dict(type='function_call_output',call_id=m['id'],output=m['text'])]+plain(response('已核对').output)))
        results.append(run_fixture(case['id'], rounds,facts,output,cfg,'继续核对当前有效事实与待办状态，不执行业务变更。',shared_budget))
    for item in manifest:
        f = json.loads((ROOT/'long_fixtures'/f"{item['id']}.json").read_text())
        oracle = json.loads((ROOT/'long_oracles'/f"{item['id']}.json").read_text())
        results.append(run_fixture(item['id'],f['rounds'],oracle['facts'],output,cfg,f['final_query'],shared_budget))
    final=dict(results=results,output=str(output),evidence_type='FAKE_PROTOCOL_ONLY',paid_calls=0,
        production_unchanged=before==hashes(),previous_evidence_unchanged=protected==protected_hashes(),
        production_hashes=before,final_gate='BLOCKED_FOR_C2',blockers=['PROVIDER_PRICE_UNCONFIRMED','REAL_INPUT_BOUND_UNCONFIRMED'],
        shared_budget_calls=shared_budget.calls,shared_reserved_input=shared_budget.inputs,shared_reserved_output=shared_budget.outputs)
    save(output/'result.json',final)
    if not (ROOT/'provider_trace_dry_run.json').exists():
        save(ROOT/'provider_trace_dry_run.json',final)


if __name__=='__main__':
    main()
