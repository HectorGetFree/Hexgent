"""获批Stage 2：独立额度、逐例落盘；MAIN始终Fake，不进入Stage 1。"""
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
import argparse
import io
import os
import socket
import unittest

from dotenv import load_dotenv
from openai import APIError
from eval.context_management.c1_independent import hashes, save
from eval.context_management.c2_dispatch_guard_repair import entry as controlled
from eval.context_management.c2_prep_3.prepare import ROOT, read_json, digest, snapshot
from eval.context_management.c2_prep_3.runner import policy, validate_capacity, load_rounds
from eval.context_management.c2_prep_3.budget import TokenBudget, require_stage_approval


ORDER=['F2-01','F3-02','F4-03','L1','L3']
STAGE1=ROOT.parent/'c2/runs/stage1-real-20261011T150121165142Z'
REVIEW=ROOT.parent/'c2/reviews/stage1-post-smoke-20261011T152803062442Z'
APPROVAL=Path('/Users/hector/.codex/attachments/90951f09-9d82-4170-b607-51000b91aa60/已粘贴的文本.txt')


def protected():
    folders=[ROOT,ROOT.parent/'c2_dispatch_guard_repair',STAGE1,
             STAGE1.parent/'stage1-real-20261011T090900010334Z',ROOT.parent/'c2/reviews']
    result=snapshot()
    for folder in folders:
        result.update({str(p):digest(p) for p in folder.rglob('*') if p.is_file() and '__pycache__' not in str(p)})
    return result


def gate(output):
    manifest=read_json(ROOT/'smoke_manifest.json')
    validate_capacity(manifest,read_json(ROOT/'capacity_confirmation.json'))
    initial=read_json(STAGE1/'execution_manifest.json')
    assert hashes()==initial['production_before'],'PRODUCTION_DRIFT'
    for p,h in {**initial['protected_before'],**initial['prep3_before'],**initial['eval_source_hashes']}.items():
        assert digest(Path(p))==h,('FROZEN_DRIFT',p)
    review=read_json(REVIEW/'stage2_readiness.json')
    assert review['gate']=='STAGE2_REVIEW_WITH_KNOWN_QUALITY_RISKS'
    approval=dict(approved=True,source_type='HUMAN_REVIEW',source_reference=str(APPROVAL),
                  source_sha256=digest(APPROVAL),stage1_run_id=STAGE1.name)
    require_stage_approval(2,approval,dict(run_id=STAGE1.name,audit_valid=True),dry_run=False)
    entries=[e for e in manifest['fixtures'] if e['stage']==2]
    assert [e['fixture_id'] for e in entries]==ORDER
    limits=policy(manifest,2)
    assert (limits.max_compaction_calls,limits.max_total_reserved_input,limits.max_total_reserved_output)==(20,524288,40000)
    for e in entries: load_rounds(e)
    before=hashes();assets=protected();log=io.StringIO();attempts=[]
    def deny(*a,**kw):attempts.append('network');raise AssertionError('离线门禁禁止网络')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{},clear=True))
        stack.enter_context(patch.object(socket.socket,'connect',deny))
        stack.enter_context(patch.object(socket.socket,'connect_ex',deny))
        stack.enter_context(patch('socket.create_connection',deny))
        modules=['eval.context_management.c2.test_serialization','eval.context_management.c2_dispatch_guard_repair.test_guard',
                 'eval.context_management.c2_prep_3.test_gate','eval.context_management.c2_prep_2.test_control',
                 'eval.context_management.c2_prep.test_prepare','eval.tool_hardening.test_v3_provider',
                 'eval.context_management.c2.test_stage2']
        tests=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(modules))
        fake_budget=TokenBudget(limits)
        dry=[controlled.execute(e,output/'offline-fixtures',fake_budget) for e in entries]
    (output/'preflight_test_results.txt').write_text(log.getvalue(),encoding='utf-8')
    result=dict(tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors),skips=len(tests.skipped),
        network_attempts=attempts,production_unchanged=before==hashes(),protected_unchanged=assets==protected(),
        fake_fixture_results=dry,approval=approval,real_calls=0,
        runner_sha256=digest(Path(__file__)))
    result['gate']='PASS' if tests.wasSuccessful() and not attempts and result['production_unchanged'] and result['protected_unchanged'] and all(r['status']=='PASS' for r in dry) else 'FAIL'
    save(output/'preflight.json',result)
    assert result['gate']=='PASS','OFFLINE_GATE_FAILED'
    return manifest,entries,approval


class Stage2Guard(controlled.DispatchGuard):
    """只加Eval熔断：授权/落盘异常后不允许后续物理派发。"""
    def authorize(self,*args,**kwargs):
        try:return super().authorize(*args,**kwargs)
        except BaseException:
            self.budget.halted_reason=self.budget.halted_reason or 'GUARD_AUTHORIZATION_FAILURE'
            raise
    def dispatch(self,*args,**kwargs):
        try:return super().dispatch(*args,**kwargs)
        except APIError:
            # 明确的SDK Provider异常仅交给原Production合法Retry，额度不返还。
            raise
        except BaseException:
            self.budget.halted_reason=self.budget.halted_reason or 'GUARD_DISPATCH_FAILURE'
            raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');parser.add_argument('--gate-dir',type=Path)
    args=parser.parse_args()
    output=ROOT.parent/'c2/runs'/('stage2-real-' if args.execute else 'stage2-gate-')
    output=output.parent/(output.name+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));output.mkdir(parents=True)
    if args.execute:
        assert args.gate_dir is not None,'OFFLINE_GATE_REQUIRED'
        prior=read_json(args.gate_dir/'preflight.json');assert prior['gate']=='PASS'
        assert digest(Path(__file__))==prior['runner_sha256'],'RUNNER_CHANGED_AFTER_GATE'
        manifest=read_json(ROOT/'smoke_manifest.json');entries=[e for e in manifest['fixtures'] if e['stage']==2]
        approval=prior['approval'];require_stage_approval(2,approval,dict(run_id=STAGE1.name,audit_valid=True))
        assert digest(APPROVAL)==approval['source_sha256']
        assert [e['fixture_id'] for e in entries]==ORDER
        initial=read_json(STAGE1/'execution_manifest.json')
        assert hashes()==initial['production_before']
        for p,h in {**initial['protected_before'],**initial['prep3_before'],**initial['eval_source_hashes']}.items():assert digest(Path(p))==h
        validate_capacity(manifest,read_json(ROOT/'capacity_confirmation.json'))
        for e in entries:load_rounds(e)
    else:
        gate(output);print(str(output),flush=True);return
    before=hashes();assets=protected();budget=TokenBudget(policy(manifest,2));client=None;results=[];error=None
    save(output/'execution_manifest.json',dict(stage=2,entries=entries,provider=manifest['provider'],approval=approval,
        stage1_run=str(STAGE1),stage1_review=str(REVIEW),preflight=str(args.gate_dir),capacity_status='USER_ASSERTED_CAPACITY_128K',
        budget=manifest['stages'][1],production_before=before,protected_before=assets,
        serialization_revision='OPTIONAL_ASSISTANT_NULL_METADATA_OMISSION_V1',
        eval_source_hashes={str(p):digest(p) for p in [Path(__file__),Path(controlled.__file__),ROOT.parent/'c2_dispatch_guard_repair/guard.py']}))
    try:
        load_dotenv(ROOT.parents[2]/'.env',override=True)
        assert os.environ.get('OPENAI_BASE_URL','').rstrip('/')==manifest['provider']['base_url'].rstrip('/'),'PROVIDER_ROUTE_MISMATCH'
        client=controlled.make_client(manifest,execute=True)
        print('OUTPUT '+str(output),flush=True)
        with patch.object(controlled,'DispatchGuard',Stage2Guard):
            for e in entries:
                print('START '+e['fixture_id'],flush=True)
                result=controlled.execute(e,output,budget,client=client,transport_mode='NETWORK');results.append(result)
                ev=read_json(output/e['fixture_id']/'evidence.json')
                assert result['attribution']['pass_'],'ATTRIBUTION_FAILURE'
                assert all(c['call_type']=='COMPACTION' or not c['actual_network_dispatch'] for c in ev['calls']),'REAL_MAIN_DISPATCH'
                assert not any(c['outcome']=='IN_FLIGHT' for c in ev['calls']),'TRACE_INCOMPLETE'
                # 每例结束立即保存总账；质量FAIL不触发重新采样，也不冒充派发错误。
                save(output/'budget_ledger.json',dict(calls=budget.calls,input_reserved=budget.inputs,output_reserved=budget.outputs,
                    reservations=budget.reservations,halted_reason=budget.halted_reason))
                print(f"DONE {e['fixture_id']} compaction={result['compaction_calls']} accepted={result['accepted']} main={result['main_fake_calls']}",flush=True)
                if budget.halted_reason:break
    except BaseException as exc:
        error=dict(type=type(exc).__name__,message=str(exc));save(output/'execution_error.json',error)
    finally:
        if client is not None:client.close()
        traces=[]
        for e in entries:
            p=output/e['fixture_id']/'provider-trace.json'
            if p.exists():traces.append(dict(fixture_id=e['fixture_id'],**read_json(p)))
        save(output/'provider_trace.json',dict(fixtures=traces))
        save(output/'budget_ledger.json',dict(calls=budget.calls,input_reserved=budget.inputs,output_reserved=budget.outputs,
            reservations=budget.reservations,halted_reason=budget.halted_reason))
        final=dict(results=results,error=error,quality='QUALITY_REVIEW_PENDING',output=str(output),
            execution_complete=len(results)==5 and error is None and not budget.halted_reason,
            production_unchanged=before==hashes(),protected_unchanged=assets==protected(),
            real_compaction_calls=sum(c['actual_network_dispatch'] for t in traces for c in t['calls']),stage1_executed=False)
        save(output/'execution_result.json',final);print(str(final),flush=True)


if __name__=='__main__':main()
