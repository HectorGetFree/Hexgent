"""离线交付验证：凭证隔离、禁止网络、保留修复前后独立证据。"""
from contextlib import ExitStack
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
import io
import json
import os
import socket
import subprocess
import unittest

from .entry import execute,make_client
from .test_guard import Rig,mock_transport
from eval.context_management.c2.stage1_preflight import probe
from eval.context_management.c2_prep_3.prepare import ROOT,read_json,snapshot,digest
from eval.context_management.c2_prep_3.runner import policy
from eval.context_management.c2_prep_3.budget import TokenBudget,require_stage_approval
from eval.context_management.c2_prep_2.control import BudgetDenied
from eval.context_management.c1_independent import hashes,save

DEST=Path(__file__).resolve().parent


def main():
    before=hashes();protected=snapshot()
    frozen={str(p):digest(p) for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
    old_stage=ROOT.parent/'c2/runs/stage1-20261011T080842980550Z'
    historical={str(p):digest(p) for p in old_stage.rglob('*') if p.is_file()}
    output=DEST/'runs'/('offline-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));output.mkdir(parents=True)
    # 保留前一次未完成验证的摘要与 ERROR 日志，不覆盖失败证据。
    for name in ('before_probe.json','after_probe.json','test_results.txt','provider_trace_review.json',
                 'mock_real_entry_run.json','production_hash_check.json','result.json'):
        if (DEST/name).exists():
            archive=output/'prior-attempt-summary';archive.mkdir(exist_ok=True)
            (DEST/name).rename(archive/name)
    network=[]
    def denied_network(*args,**kwargs):
        network.append('BLOCKED_NETWORK_ATTEMPT');raise AssertionError('本轮禁止网络')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{},clear=True))
        stack.enter_context(patch.object(socket.socket,'connect',denied_network))
        stack.enter_context(patch('socket.create_connection',denied_network))
        stack.enter_context(patch('hexgent.client.get_client',side_effect=AssertionError('本轮禁止凭证入口')))
        # 1. 重新运行原缺陷，而不是引用历史 PASS/FAIL。
        prior=output/'before';prior.mkdir();before_probe=probe(prior);save(DEST/'before_probe.json',before_probe)
        afterdir=output/'after';afterdir.mkdir();r=Rig(afterdir)
        with r.observed():
            h=r.begin();r.call();r.guard.end(h,status='SUCCEEDED')
            reason=None
            try:r.call()
            except BudgetDenied as error:reason=str(error)
        after_probe=dict(native_fake_calls=len(r.wires),second_call_native_dispatches=len(r.wires)-1,
            second_call_denial=reason,real_provider_calls=0,status='PASS' if len(r.wires)==1 and reason else 'FAIL')
        save(DEST/'after_probe.json',after_probe)
        # 2. 本轮重新执行新旧相关回归，输出真实测试日志。
        modules=['eval.context_management.c2_dispatch_guard_repair.test_guard',
            'eval.context_management.c2_prep_3.test_gate','eval.context_management.c2_prep_2.test_control',
            'eval.context_management.c2_prep.test_prepare','eval.tool_hardening.test_v3_provider']
        suite=unittest.defaultTestLoader.loadTestsFromNames(modules);log=io.StringIO()
        tests=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
        (DEST/'test_results.txt').write_text(log.getvalue(),encoding='utf-8')
        if not tests.wasSuccessful(): raise RuntimeError('OFFLINE_REGRESSION_FAILED')
        manifest=read_json(ROOT/'smoke_manifest.json')
        # 3. 七条 Fake Dry Run 仅写新目录；没有调用旧 main 或修改冻结结果。
        dry=output/'seven-fake';dry.mkdir();dry_results=[]
        for stage in (1,2):
            budget=TokenBudget(policy(manifest,stage))
            for e in manifest['fixtures']:
                if e['stage']==stage:dry_results.append(execute(e,dry,budget))
        save(DEST/'provider_trace_review.json',dict(seven_fake_results=dry_results,output=str(dry),
            quality='NOT_EVALUATED_FAKE_RESPONSE',all_pass=all(r['status']=='PASS' for r in dry_results)))
        # 4. 与真实入口完全相同的 SDK 构造和 Production Loop，网络用 MockTransport 替代。
        mockdir=output/'mock-stage1';mockdir.mkdir();budget=TokenBudget(policy(manifest,1));wires=[];mock_results=[]
        for e in manifest['fixtures'][:2]:
            oracle=read_json(Path(e['oracle_v2_path']))
            summary='<summary>'+'\n'.join(f['quote'] for f in oracle['facts'] if f['summary_responsibility']=='SUMMARY_REQUIRED')+'</summary>'
            client=make_client(manifest,execute=True,mock_transport=mock_transport(summary,wires))
            try:mock_results.append(execute(e,mockdir,budget,client=client,transport_mode='MOCK_TRANSPORT'))
            finally:client.close()
        save(DEST/'mock_real_entry_run.json',dict(results=mock_results,mock_http_requests=wires,
            sdk_max_retries=0,eval_retries=0,output=str(mockdir),actual_network_calls=0,
            scope='SDK入口接线证据，不是实际Provider可用性或Usage证明',reservations=budget.reservations))
        try:require_stage_approval(2)
        except BudgetDenied:stage2_denied=True
        else:stage2_denied=False
    diff=subprocess.run(['/usr/bin/git','diff','--check'],capture_output=True,text=True)
    checks=dict(production_unchanged=before==hashes(),production_before=before,production_after=hashes(),
        protected_assets_unchanged=protected==snapshot(),protected_before=protected,
        prep3_unchanged=all(Path(p).exists() and digest(Path(p))==h for p,h in frozen.items()),prep3_before=frozen,
        original_stage1_unchanged=all(Path(p).exists() and digest(Path(p))==h for p,h in historical.items()),
        original_stage1_before=historical,git_diff_check_exit_code=diff.returncode,git_diff_check_output=diff.stdout+diff.stderr)
    save(DEST/'production_hash_check.json',checks)
    passed=all([before_probe['stale_span_reused'],after_probe['status']=='PASS',tests.wasSuccessful(),
        all(r['status']=='PASS' for r in dry_results+mock_results),stage2_denied,not network,
        checks['production_unchanged'],checks['protected_assets_unchanged'],checks['prep3_unchanged'],checks['original_stage1_unchanged'],diff.returncode==0])
    result=dict(gate='READY_FOR_STAGE1_REAL_DISPATCH_REVIEW' if passed else 'BLOCKED_FOR_STAGE1',
        tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors),skips=len(tests.skipped),
        seven_fake_pass=sum(r['status']=='PASS' for r in dry_results),mock_stage1_pass=sum(r['status']=='PASS' for r in mock_results),
        real_provider_calls=0,network_attempts=network,stage2_denied_without_review=stage2_denied,output=str(output))
    save(DEST/'result.json',result);print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
