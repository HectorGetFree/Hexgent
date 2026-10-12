"""Stage 1 派发前审计：只使用无网络探针，发现归因缺口立即停止。"""
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import subprocess
import sys

from eval.context_management.c1_independent import hashes, response, save
from eval.context_management.c2_prep_3.prepare import ROOT, digest, snapshot, read_json
from eval.context_management.c2_prep_3.runner import load_rounds, validate_capacity, policy
from eval.context_management.c2_prep_3.budget import TokenBudget, TokenTrace
from eval.context_management.c2_prep_3.test_gate import small_policy, REQUEST
from hexgent.observation import RunObservation, current_observation, begin_span, end_span


def probe(output):
    """1. 正常请求结束后，不创建新 Span，再调用同一真实 Trace 边界。"""
    calls=[]
    def fake_create(**request):
        calls.append(request)
        return response('<summary>无网络探针</summary>')
    native=SimpleNamespace(responses=SimpleNamespace(create=fake_create),max_retries=0,base_url='offline://probe')
    directory=output/'preflight-probe'; directory.mkdir()
    store=SimpleNamespace(session_dir=directory,session_id='OFFLINE-S')
    session=SimpleNamespace(store=store,_active_run_id='OFFLINE-R')
    observer=RunObservation(store); token=current_observation.set(observer)
    budget=TokenBudget(small_policy())
    trace=TokenTrace(native,directory/'provider-trace.json',budget.bind('F'),eval_run_id=output.name,fixture_id='F',session=session)
    handle=begin_span('provider',request_type='COMPACTION',attempt=1)
    try:
        first=trace.responses.create(model='OFFLINE',**REQUEST)
        end_span(handle,status='SUCCEEDED',response=first)
        # 2. 无新 provider_started：应拒绝，而不是借用已结束的 Span。
        denied=None
        try: trace.responses.create(model='OFFLINE',**REQUEST)
        except Exception as error: denied={'type':type(error).__name__,'message':str(error)}
    finally: current_observation.reset(token)
    rows=trace.state['calls']
    result=dict(expected='SECOND_CALL_REJECTED_BEFORE_NATIVE_CREATE',
        observed_native_fake_calls=len(calls),real_provider_calls=0,
        second_call_denial=denied,span_ids=[r['span_id'] for r in rows],
        reservation_ids=[r['reservation']['reservation_id'] for r in rows],
        stale_span_reused=len(rows)==2 and rows[0]['span_id']==rows[1]['span_id'],
        budget_reserved_calls=budget.calls,
        scope='Eval attribution boundary; no Production defect claimed')
    save(directory/'result.json',result)
    return result


def main():
    output=ROOT.parent/'c2'/'runs'/('stage1-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    production=hashes(); protected=snapshot()
    frozen={str(p):digest(p) for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
    manifest=read_json(ROOT/'smoke_manifest.json'); confirmation=read_json(ROOT/'capacity_confirmation.json')
    validate_capacity(manifest,confirmation)
    entries=[e for e in manifest['fixtures'] if e['stage']==1]
    assert [e['fixture_id'] for e in entries]==['F1-02','L2']
    for entry in entries: load_rounds(entry)
    budget=policy(manifest,1)
    assert (budget.max_compaction_calls,budget.max_total_reserved_input,budget.max_total_reserved_output)==(8,262144,16000)
    save(output/'execution_manifest.json',dict(stage=1,fixture_ids=['F1-02','L2'],
        capacity_status='USER_ASSERTED_CAPACITY_128K',real_dispatch_permitted=False,
        manifest_hash=digest(ROOT/'smoke_manifest.json'),confirmation_hash=digest(ROOT/'capacity_confirmation.json'),
        frozen_entries=entries,production_hashes=production,protected_hashes=protected,prep3_hashes=frozen))
    result=probe(output)
    # 3. 已确认的 Gate A-9 缺口触发停止，不构建真实 SDK Client。
    assert result['stale_span_reused'] and result['observed_native_fake_calls']==2
    command=[sys.executable,'-m','unittest','eval.context_management.c2_prep_3.test_gate',
        'eval.context_management.c2_prep_2.test_control','eval.context_management.c2_prep.test_prepare',
        'eval.tool_hardening.test_v3_provider','-v']
    test=subprocess.run(command,capture_output=True,text=True)
    (output/'test_results.txt').write_text(test.stdout+test.stderr,encoding='utf-8')
    diff=subprocess.run(['git','diff','--check'],capture_output=True,text=True)
    final=dict(gate='BLOCKED_BEFORE_REAL_DISPATCH',real_compaction_provider_calls=0,
        stage1_fixture_results={i:'NOT_RUN' for i in ['F1-02','L2']},blocking_gate='A-9',probe=result,
        production_unchanged=production==hashes(),protected_assets_unchanged=protected==snapshot(),
        prep3_unchanged=all(p.exists() and digest(p)==v for k,v in frozen.items() for p in [Path(k)]),
        regression_exit_code=test.returncode,git_diff_check_exit_code=diff.returncode,
        git_diff_check_output=diff.stdout+diff.stderr)
    save(output/'result.json',final)
    save(output/'provider_trace.json',dict(real_provider_calls=[],offline_probe='preflight-probe/provider-trace.json'))
    save(output/'budget_ledger.json',dict(stage1_actual_calls=0,input_reserved=0,output_reserved=0,budget_exceeded=False,
        note='隔离探针的两次 Fake 预留不计入真实 Stage 1 额度'))
    save(output/'quality_review.json',dict(status='NOT_EVALUATED_NO_REAL_DISPATCH',fact_recall=None,stale_activation=None,unsupported_claims=None))
    save(output/'token_metrics.json',dict(status='NOT_AVAILABLE_NO_REAL_DISPATCH',provider_usage=None,pre_tokens=None,post_tokens=None))
    for identifier in ['F1-02','L2']:
        (output/identifier).mkdir(); save(output/identifier/'result.json',dict(status='NOT_RUN',reason='Gate A-9 failed'))
    print(json.dumps(dict(output=str(output),**final),ensure_ascii=False))


if __name__=='__main__': main()
