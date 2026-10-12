"""Stage 1 失败后的只读归因与离线回归，不调用真实模型。"""
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import argparse
import io
import json
import os
import socket
import subprocess
import unittest

from eval.context_management.c1_independent import save,hashes
from eval.context_management.c2_prep_3.prepare import read_json,digest
from hexgent.utils.token_estimator import estimate_request_tokens


def main():
    parser=argparse.ArgumentParser();parser.add_argument('run',type=Path);args=parser.parse_args();run=args.run
    initial=read_json(run/'execution_manifest.json');ledger=read_json(run/'budget_ledger.json')
    evidence=read_json(run/'F1-02/evidence.json');calls=evidence['calls']
    comp=[c for c in calls if c['call_type']=='COMPACTION'];main=[c for c in calls if c['call_type']=='MAIN']
    assert len(comp)==2 and all(c['outcome']=='ERROR' for c in comp)
    assert all('logprobs' in str(c['reason']) and '400' in str(c['reason']) for c in comp)
    pre=next(e['before_tokens'] for e in evidence['events'] if 'before_tokens' in e)
    finalreq=main[0]['request'];post=estimate_request_tokens(finalreq['instructions'],finalreq['input'],finalreq.get('tools',[]))
    save(run/'token_metrics.json',dict(fixtures=[dict(fixture_id='F1-02',pre_compaction_full_request_estimated_input_tokens=pre,
        post_compaction_full_request_estimated_input_tokens=post,estimated_request_reduction=pre-post,
        estimated_request_reduction_ratio=(pre-post)/pre,accepted_compaction=False,
        note='未产生压缩；post 为失败后保留原历史的 Fake MAIN 请求估算',
        compaction_provider_input_tokens=None,compaction_provider_output_tokens=None,compaction_provider_total_tokens=None,
        provider_usage_complete=False,usage_status='MISSING_HTTP_400',compaction_create_count=len(comp)),
        dict(fixture_id='L2',status='NOT_RUN',provider_usage_complete=None)],
        monetary_cost='MONETARY_COST_NOT_EVALUATED'))
    # 未生成任何摘要，不得将 Provider 拒绝误算为摘要 Fact Recall=0。
    save(run/'quality_review.json',dict(status='NOT_EVALUATED_PROVIDER_SCHEMA_REJECTION',review_method='基于原始请求和错误的独立人工归因，未运行 Judge 模型',
        fixtures=[dict(fixture_id='F1-02',summary_required_denominator=5,fact_recall=None,
            stale_fact_activation=None,unsupported_claims=None,candidate_count=0,
            source_detail='冻结 Eval load_rounds 使用 response → plain，assistant output_text 序列化含 logprobs:null',
            error_param='input[3].content[0].logprobs'),dict(fixture_id='L2',status='NOT_RUN',summary_required_denominator=4,fact_recall=None)],
        main_request_information_available=True,note='原历史保留不等于摘要保留成功'))
    save(run/'provider_attribution_review.json',dict(real_compaction_provider_calls=len(comp),fake_main_calls=len(main),
        raw_actual_network_calls_field=read_json(run/'execution_result.json')['results'][0]['actual_network_calls'],
        corrected_real_provider_calls=len(comp),correction='Guard 的 actual_network_dispatch 标志未按 call_type 排除 Fake MAIN；保留原始字段，依真实入口分支与 HTTP400 校正统计',
        errors=[dict(provider_call_id=c['provider_call_id'],span_id=c['span_id'],request_hash=c['request_hash'],reason=c['reason']) for c in comp],
        sdk_retries=0,eval_retries=0,production_retries=1,timeouts=0,
        missing_evidence=['Provider response headers/request ID 未保存；SDK 异常类型和完整错误消息已保存']))
    log=io.StringIO();network=[]
    def block(*a,**kw):network.append('attempt');raise AssertionError('回归禁止网络')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{},clear=True))
        stack.enter_context(patch.object(socket.socket,'connect',block))
        stack.enter_context(patch('socket.create_connection',block))
        modules=['eval.context_management.c2_dispatch_guard_repair.test_guard','eval.context_management.c2_prep_3.test_gate',
            'eval.context_management.c2_prep_2.test_control','eval.context_management.c2_prep.test_prepare','eval.tool_hardening.test_v3_provider']
        tests=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(modules))
    (run/'test_results.txt').write_text(log.getvalue(),encoding='utf-8')
    diff=subprocess.run(['/usr/bin/git','diff','--check'],capture_output=True,text=True)
    complete=lambda group:all(Path(p).exists() and digest(Path(p))==h for p,h in group.items())
    checks=dict(production_unchanged=hashes()==initial['production_before'],production_after=hashes(),
        protected_unchanged=complete(initial['protected_before']),prep3_unchanged=complete(initial['prep3_before']),
        git_diff_check_exit_code=diff.returncode,git_diff_check_output=diff.stdout+diff.stderr)
    save(run/'hash_check.json',checks)
    result=dict(gate='STAGE1_INCOMPLETE',reason='FIXTURE_PROVIDER_SCHEMA_INCOMPATIBILITY',real_compaction_calls=len(comp),
        F1_02='PROVIDER_REQUEST_REJECTED',L2='NOT_RUN',budget_exceeded=False,
        input_reserved=ledger['input_reserved'],output_reserved=ledger['output_reserved'],
        regression=dict(tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors),skips=len(tests.skipped)),
        regression_network_attempts=network,**checks)
    save(run/'result.json',result)
    print(json.dumps({k:v for k,v in result.items() if k!='production_after'},ensure_ascii=False))


if __name__=='__main__':main()
