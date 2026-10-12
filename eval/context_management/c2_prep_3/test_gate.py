"""Token预算、Oracle责任和人工审批的离线测试，不访问真实Provider。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
import json
import httpx
from openai import OpenAI

from .budget import TokenBudget,TokenPolicy,TokenTrace,require_stage_approval
from .prepare import ROOT,read_json,ORDER
from .runner import policy,execute,validate_capacity
from eval.context_management.c2_prep_2.control import BudgetDenied
from eval.context_management.c2_prep.prepare import StreamRecorder
from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS


def small_policy(**changes):
    return replace(TokenPolicy(2,10000,4000,{'F':dict(max_reserved_input_per_call=10000,max_output_tokens_per_call=2000,max_compaction_calls=2)}),**changes)


REQUEST=dict(instructions=COMPACTION_INSTRUCTIONS,input=[{'role':'user','content':'核对'}],max_output_tokens=2000)


class BudgetTests(unittest.TestCase):
    def test_normal_without_prices(self):
        b=TokenBudget(small_policy()); row=b.reserve('F',REQUEST)
        self.assertEqual(b.calls,1); self.assertGreater(row['input_reserved_tokens'],row['input_estimated_tokens'])
        self.assertEqual(b.outputs,2000)
    def test_input_cap(self):
        b=TokenBudget(small_policy()); request=dict(REQUEST,input=[{'role':'user','content':'大'*10000}])
        with self.assertRaisesRegex(BudgetDenied,'PER_CALL_ESTIMATED_INPUT'):b.reserve('F',request)
        self.assertEqual(b.calls,0)
    def test_output_cap(self):
        b=TokenBudget(small_policy())
        with self.assertRaisesRegex(BudgetDenied,'PER_CALL_OUTPUT'):b.reserve('F',dict(REQUEST,max_output_tokens=2001))
        self.assertEqual(b.calls,0)
    def test_total_input_cap(self):
        first=TokenBudget(small_policy()).reserve('F',REQUEST)['input_reserved_tokens']
        b=TokenBudget(small_policy(max_total_reserved_input=first));b.reserve('F',REQUEST)
        with self.assertRaisesRegex(BudgetDenied,'GLOBAL_INPUT'):b.reserve('F',REQUEST)
    def test_total_output_cap(self):
        b=TokenBudget(small_policy(max_total_reserved_output=2000));b.reserve('F',REQUEST)
        with self.assertRaisesRegex(BudgetDenied,'GLOBAL_OUTPUT'):b.reserve('F',REQUEST)
    def test_global_call_cap(self):
        b=TokenBudget(small_policy(max_compaction_calls=1));b.reserve('F',REQUEST)
        with self.assertRaisesRegex(BudgetDenied,'GLOBAL_CALL'):b.reserve('F',REQUEST)
    def test_fixture_cap_and_no_extra(self):
        b=TokenBudget(small_policy());b.reserve('F',REQUEST);b.reserve('F',REQUEST)
        with self.assertRaisesRegex(BudgetDenied,'FIXTURE_CALL'):b.reserve('F',REQUEST)
        self.assertEqual(b.calls,2)
    def test_concurrent_inflight(self):
        b=TokenBudget(small_policy(max_compaction_calls=1))
        def reserve(_):
            try:b.reserve('F',REQUEST);return True
            except BudgetDenied:return False
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(reserve,range(4))),1)
    def test_unknown_fixture_rejected(self):
        with self.assertRaisesRegex(BudgetDenied,'FIXTURE_NOT'):TokenBudget(small_policy()).reserve('UNKNOWN',REQUEST)
    def test_fake_main_not_charged(self):
        b=TokenBudget(small_policy());b.reserve('F',dict(REQUEST,instructions='SYSTEM'))
        self.assertEqual((b.calls,b.inputs,b.outputs),(0,0,0))


class OracleAndApprovalTests(unittest.TestCase):
    def test_all_108_and_only_one_channel_change(self):
        m=read_json(ROOT/'oracle_migration.json')
        self.assertEqual(m['checked'],108);self.assertEqual(len(m['changes']),1)
        self.assertEqual(m['changes'][0]['fact_id'],'L2:fact:7')
        for path in (ROOT/'oracle_v2').glob('*.json'):
            v2=read_json(path);original=read_json(Path(v2['original_oracle']))
            for before,after in zip(original['facts'],v2['facts']):
                self.assertTrue(all(after[k]==value for k,value in before.items()))
    def test_recent_tail_not_recall_denominator(self):
        oracle=read_json(ROOT/'oracle_v2/L2.json')
        fact=next(f for f in oracle['facts'] if f['fact_id']=='L2:fact:7')
        self.assertFalse(fact['summary_input_present']);self.assertTrue(fact['recent_tail_present'])
        self.assertTrue(fact['final_request_present'])
        self.assertEqual(sum(f['summary_responsibility']=='SUMMARY_REQUIRED' for f in oracle['facts']),4)
    def test_seven_order_and_hashes(self):
        from .prepare import digest
        manifest=read_json(ROOT/'smoke_manifest.json')
        self.assertEqual([e['fixture_id'] for e in manifest['fixtures']],ORDER)
        for e in manifest['fixtures']:
            self.assertEqual(digest(Path(e['fixture_path'])),e['fixture_hash'])
            self.assertEqual(digest(Path(e['oracle_v2_path'])),e['oracle_v2_hash'])
    def test_stage2_no_approval(self):
        with self.assertRaisesRegex(BudgetDenied,'HUMAN_APPROVAL'):require_stage_approval(2)
    def test_offline_approval_cannot_authorize_real_stage2(self):
        a={'approved':True,'stage1_run_id':'A','evidence_type':'OFFLINE_APPROVAL_GATE_TEST'}
        with self.assertRaisesRegex(BudgetDenied,'HUMAN_EVIDENCE'):require_stage_approval(2,a,{'run_id':'A','audit_valid':True})
        require_stage_approval(2,a,{'run_id':'A','audit_valid':True},dry_run=True)
    def test_approval_bound_to_prior_run(self):
        a={'approved':True,'stage1_run_id':'A','source_type':'HUMAN_REVIEW','source_reference':'用户审批记录'}
        with self.assertRaisesRegex(BudgetDenied,'REVIEW_NOT_BOUND'):require_stage_approval(2,a,{'run_id':'B','audit_valid':True})
        require_stage_approval(2,a,{'run_id':'A','audit_valid':True})
    def test_capacity_pending_denied_confirmed_accepted(self):
        manifest=read_json(ROOT/'smoke_manifest.json'); c=read_json(ROOT/'capacity_confirmation.json')
        with self.assertRaisesRegex(RuntimeError,'CAPACITY_NOT_CONFIRMED'):validate_capacity(manifest,dict(c,confirmed=False))
        validate_capacity(manifest,c)


class TraceTests(unittest.TestCase):
    def entry(self):return read_json(ROOT/'smoke_manifest.json')['fixtures'][0]
    def test_retry_all_attempts_reserved(self):
        entry=self.entry();p=policy(read_json(ROOT/'smoke_manifest.json'),1);b=TokenBudget(p)
        with TemporaryDirectory() as tmp:
            result=execute(entry,Path(tmp),b,summary_override=['无summary格式'])
            self.assertEqual(result['compaction_calls'],2);self.assertEqual(b.calls,2)
            rows=json.loads((Path(tmp)/entry['fixture_id']/'provider-trace.json').read_text())['calls']
            self.assertEqual([r['retry_index'] for r in rows if r['call_type']=='COMPACTION'],[0,1])
    def test_retry_budget_exhaustion_zero_extra_dispatch(self):
        manifest=read_json(ROOT/'smoke_manifest.json');entry=self.entry()
        p=policy(manifest,1);p=replace(p,fixtures={entry['fixture_id']:dict(entry,max_compaction_calls=1)})
        b=TokenBudget(p)
        with TemporaryDirectory() as tmp:
            result=execute(entry,Path(tmp),b,summary_override=['无summary格式'])
            self.assertEqual(result['compaction_calls'],1);self.assertEqual(b.calls,1)
            self.assertEqual(b.halted_reason,'FIXTURE_CALL_LIMIT')
    def test_missing_usage_retains_reservation_not_zero(self):
        original=StreamRecorder.create
        def missing(obj,**request):
            for event in original(obj,**request):event.response.usage=None;yield event
        b=TokenBudget(policy(read_json(ROOT/'smoke_manifest.json'),1));entry=self.entry()
        with TemporaryDirectory() as tmp,patch.object(StreamRecorder,'create',missing):
            result=execute(entry,Path(tmp),b)
            self.assertEqual(result['status'],'PASS');self.assertGreater(b.inputs,0);self.assertEqual(b.calls,1)
            rows=json.loads((Path(tmp)/entry['fixture_id']/'provider-trace.json').read_text())['calls']
            self.assertTrue(all(r['input_tokens'] is None and r['usage_status']=='MISSING' for r in rows))
            self.assertIsNone(b.halted_reason)
    def test_missing_attribution_zero_native_dispatch(self):
        calls=[];b=TokenBudget(small_policy())
        fake=SimpleNamespace(responses=SimpleNamespace(create=lambda **kw:calls.append(kw)),max_retries=0)
        with TemporaryDirectory() as tmp:
            t=TokenTrace(fake,Path(tmp)/'trace.json',b.bind('F'),eval_run_id='test',fixture_id='F')
            with self.assertRaisesRegex(BudgetDenied,'ATTRIBUTION'):t.responses.create(**REQUEST)
            self.assertEqual(calls,[])
    def test_sdk_retry_zero_mock_transport(self):
        wires=[]
        def handler(request):wires.append(request);return httpx.Response(500,json={'error':{'message':'offline fault'}})
        sdk=OpenAI(api_key='OFFLINE-NOT-SECRET',base_url='https://offline.invalid/v1',http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        from hexgent.observation import RunObservation,current_observation,begin_span,end_span
        with TemporaryDirectory() as tmp:
            store=SimpleNamespace(session_dir=Path(tmp),session_id='S'); session=SimpleNamespace(store=store,_active_run_id='R')
            observation=RunObservation(store); token=current_observation.set(observation)
            handle=begin_span('provider',request_type='COMPACTION',attempt=1)
            try:
                b=TokenBudget(small_policy());t=TokenTrace(sdk,Path(tmp)/'trace.json',b.bind('F'),eval_run_id='offline',fixture_id='F',session=session)
                with self.assertRaisesRegex(BudgetDenied,'SDK_RETRY'):t.responses.create(model='fake',**REQUEST)
                self.assertEqual(len(wires),0)
                t=t.with_options(max_retries=0)
                with self.assertRaises(Exception):t.responses.create(model='fake',**REQUEST)
                self.assertEqual(len(wires),1);self.assertEqual(len(t.state['calls']),1)
            finally:end_span(handle,status='FAILED');current_observation.reset(token);sdk.close()
