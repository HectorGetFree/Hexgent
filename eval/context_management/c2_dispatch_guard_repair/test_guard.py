"""无网络的派发前门禁、并发和 SDK 接线回归。"""
from dataclasses import replace
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from threading import Barrier
from unittest.mock import patch
import json
import unittest
import httpx

from .guard import DispatchGuard, GuardedTrace
from .entry import make_client, execute
from eval.context_management.c2_prep_3.test_gate import small_policy, REQUEST
from eval.context_management.c2_prep_3.budget import TokenBudget, require_stage_approval
from eval.context_management.c2_prep_3.runner import policy
from eval.context_management.c2_prep_3.prepare import ROOT, read_json
from eval.context_management.c1_independent import response, plain
from eval.context_management.c2_prep_2.control import BudgetDenied
from hexgent.observation import RunObservation,current_observation


class Rig:
    def __init__(self,directory,limits=None):
        self.wires=[]; self.failure=False
        def create(**request):
            self.wires.append(request)
            if self.failure: raise TimeoutError('OFFLINE_TIMEOUT')
            return response('<summary>离线</summary>')
        self.native=SimpleNamespace(responses=SimpleNamespace(create=create),max_retries=0,base_url='offline://fake')
        self.budget=TokenBudget(limits or small_policy());self.guard=DispatchGuard(self.budget)
        self.store=SimpleNamespace(session_dir=directory,session_id='S')
        self.session=SimpleNamespace(store=self.store,_active_run_id='R');self.obs=RunObservation(self.store)
        self.trace=GuardedTrace(self.native,directory/'trace.json',self.budget.bind('F'),session=self.session,
            eval_run_id='offline',fixture_id='F',guard=self.guard,main_fake=SimpleNamespace(create=create))
        self.request=dict(REQUEST,model='gpt-6.1-sol')
    @contextmanager
    def observed(self):
        token=current_observation.set(self.obs)
        try: yield
        finally: current_observation.reset(token)
    def begin(self,**changes):return self.guard.begin('provider',**dict(request_type='COMPACTION',attempt=1,**changes))
    def call(self):return self.trace.responses.create(**self.request)


class GuardTests(unittest.TestCase):
    def setUp(self):self.tmp=TemporaryDirectory();self.r=Rig(Path(self.tmp.name))
    def tearDown(self):self.tmp.cleanup()
    def rejected(self,code,function):
        before=len(self.r.wires)
        with self.assertRaisesRegex(BudgetDenied,code):function()
        self.assertEqual(len(self.r.wires)-before,0)
    def test_valid_span(self):
        with self.r.observed():self.r.begin();self.r.call()
        self.assertEqual(len(self.r.wires),1)
    def test_ended_span(self):
        with self.r.observed():
            h=self.r.begin(); b=self.r.guard.binding.get();self.r.guard.end(h,status='SUCCEEDED')
            self.r.guard.binding.set(b);self.rejected('SPAN_ENDED',self.r.call)
    def test_missing_span(self):
        with self.r.observed():self.rejected('SPAN_BINDING_MISSING',self.r.call)
    def test_other_execution(self):
        with self.r.observed():
            self.r.begin();token=current_observation.set(RunObservation(self.r.store))
            try:self.rejected('EXECUTION_MISMATCH',self.r.call)
            finally:current_observation.reset(token)
    def test_other_session(self):
        with self.r.observed():
            self.r.begin();self.r.trace.session=SimpleNamespace(store=SimpleNamespace(session_id='X'),_active_run_id='X')
            self.rejected('SESSION_MISMATCH',self.r.call)
    def test_unknown_span(self):
        with self.r.observed():
            self.r.begin();self.r.guard.binding.set(replace(self.r.guard.binding.get(),span_id='UNKNOWN'))
            self.rejected('SPAN_UNKNOWN',self.r.call)
    def test_type_mismatch(self):
        with self.r.observed():
            self.r.begin();self.r.request['instructions']='SYSTEM';self.rejected('REQUEST_TYPE_MISMATCH',self.r.call)
    def test_attempt_mismatch(self):
        with self.r.observed():
            self.r.begin();self.r.guard.binding.set(replace(self.r.guard.binding.get(),logical_attempt=2))
            self.rejected('ATTEMPT_OR_BINDING_MISMATCH',self.r.call)
    def test_duplicate_attempt(self):
        with self.r.observed():self.r.begin();self.r.call();self.rejected('ATTEMPT_ALREADY_CLAIMED',self.r.call)
    def test_same_span_explicit_new_physical_attempt(self):
        with self.r.observed():
            self.r.begin();self.r.failure=True
            with self.assertRaises(TimeoutError):self.r.call()
            self.r.failure=False
            with self.r.guard.new_physical_attempt():self.r.call()
        rows=self.r.trace.state['calls']
        self.assertEqual(rows[0]['span_id'],rows[1]['span_id'])
        self.assertEqual([r['physical_attempt'] for r in rows],[1,2])
        self.assertNotEqual(rows[0]['reservation']['reservation_id'],rows[1]['reservation']['reservation_id'])
        self.assertNotEqual(rows[0]['provider_call_id'],rows[1]['provider_call_id'])
    def test_retry_after_success_not_authorized(self):
        with self.r.observed():
            self.r.begin();self.r.call()
            def retry():
                with self.r.guard.new_physical_attempt():self.r.call()
            self.rejected('RETRY_NOT_AUTHORIZED',retry)
    def test_consumed_reservation_rejected(self):
        with self.r.observed():
            self.r.begin();self.r.call();row=self.r.trace.state['calls'][0]
            self.rejected('RESERVATION_ALREADY_CONSUMED',lambda:self.r.guard.dispatch(self.r.trace,row,self.r.request))
    def test_serialized_consumed_flag_cannot_reauthorize(self):
        with self.r.observed():
            self.r.begin();self.r.call();row=self.r.trace.state['calls'][0]
            row['reservation']['consumed']=False
            self.rejected('RESERVATION_ALREADY_CONSUMED',lambda:self.r.guard.dispatch(self.r.trace,row,self.r.request))
    def test_foreign_reservation(self):
        with self.r.observed():
            self.r.begin();row=self.r.guard.authorize(self.r.trace,self.r.request);foreign=dict(row)
            self.rejected('RESERVATION_OWNER_MISMATCH',lambda:self.r.guard.dispatch(self.r.trace,foreign,self.r.request))
    def test_changed_request(self):
        with self.r.observed():
            self.r.begin();row=self.r.guard.authorize(self.r.trace,self.r.request)
            changed=dict(self.r.request,input=[])
            self.rejected('RESERVATION_REQUEST_MISMATCH',lambda:self.r.guard.dispatch(self.r.trace,row,changed))
    def test_budget_zero_dispatch(self):
        self.r.budget.policy=replace(self.r.budget.policy,max_compaction_calls=0)
        with self.r.observed():self.r.begin();self.rejected('GLOBAL_CALL_LIMIT',self.r.call)
    def test_failure_retry_budget_retained(self):
        self.r.budget.policy=replace(self.r.budget.policy,max_compaction_calls=1)
        with self.r.observed():
            self.r.begin();self.r.failure=True
            with self.assertRaises(TimeoutError):self.r.call()
            with self.r.guard.new_physical_attempt():self.rejected('GLOBAL_CALL_LIMIT',self.r.call)
        self.assertGreater(self.r.budget.inputs,0);self.assertEqual(self.r.budget.calls,1)
    def test_sdk_retry_rejected(self):
        self.r.native.max_retries=2
        with self.r.observed():self.r.begin();self.rejected('SDK_RETRY_NOT_DISABLED',self.r.call)
    def test_observation_failure_zero_dispatch(self):
        with self.r.observed():
            self.r.begin();self.r.obs.available=False;self.rejected('OBSERVATION_UNAVAILABLE',self.r.call)
    def test_persist_failure_zero_dispatch_retained_budget(self):
        with self.r.observed(),patch.object(self.r.trace,'persist',side_effect=OSError('offline disk failure')):
            self.r.begin()
            with self.assertRaises(OSError):self.r.call()
        self.assertEqual(self.r.wires,[]);self.assertEqual(self.r.budget.calls,1)
    def test_same_reservation_concurrent_consumption(self):
        with self.r.observed():self.r.begin();b=self.r.guard.binding.get();row=self.r.guard.authorize(self.r.trace,self.r.request)
        barrier=Barrier(2)
        def consume(_):
            with self.r.observed():
                self.r.guard.binding.set(b);barrier.wait()
                try:self.r.guard.dispatch(self.r.trace,row,self.r.request);return True
                except BudgetDenied:return False
        with ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(consume,range(2))),1)
        self.assertEqual(len(self.r.wires),1)
    def test_different_spans_concurrent(self):
        barrier=Barrier(2)
        def call(_):
            with self.r.observed():
                h=self.r.begin();barrier.wait();self.r.call();self.r.guard.end(h,status='SUCCEEDED');return h[2]
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(call,range(2)))
        self.assertEqual({r['span_id'] for r in self.r.trace.state['calls']},set(ids))
        self.assertEqual(len(self.r.wires),2)
    def test_default_real_entry_disabled_before_credentials(self):
        with patch('eval.context_management.c2_dispatch_guard_repair.entry.os.environ',{}):
            with self.assertRaisesRegex(RuntimeError,'REAL_ENTRY_DISABLED'):make_client(read_json(ROOT/'smoke_manifest.json'))
    def test_stage2_human_review_still_required(self):
        with self.assertRaisesRegex(BudgetDenied,'HUMAN_APPROVAL'):require_stage_approval(2)


def mock_transport(summary,wires):
    def handle(request):
        body=json.loads(request.content);wires.append(body)
        payload={'id':'mock-c2','object':'response','created_at':0,'status':'completed','model':body['model'],
            'output':plain(response(summary).output),'usage':{'input_tokens':100,'output_tokens':80,'total_tokens':180}}
        if not body.get('stream'):return httpx.Response(200,json=payload)
        data='event: response.completed\ndata: '+json.dumps({'type':'response.completed','response':payload})+'\n\n'
        return httpx.Response(200,headers={'content-type':'text/event-stream'},content=data.encode())
    return httpx.MockTransport(handle)


class EntryTests(unittest.TestCase):
    def test_sdk_options_and_copy_share_guard(self):
        manifest=read_json(ROOT/'smoke_manifest.json');wires=[]
        client=make_client(manifest,execute=True,mock_transport=mock_transport('<summary>离线</summary>',wires))
        try:
            with TemporaryDirectory() as tmp:
                rig=Rig(Path(tmp));rig.trace.native=client
                newer=rig.trace.with_options(timeout=2); copied=rig.trace.copy(timeout=3)
                self.assertIs(newer.guard,rig.trace.guard);self.assertIs(copied.state,rig.trace.state)
                self.assertEqual(newer.native.max_retries,0)
                with rig.observed():
                    rig.begin();newer.responses.create(**rig.request)
                    with self.assertRaisesRegex(BudgetDenied,'ATTEMPT_ALREADY_CLAIMED'):copied.responses.create(**rig.request)
                self.assertEqual(len(wires),1)
        finally:client.close()
    def test_mock_real_sdk_entry_stage1_production_loop(self):
        manifest=read_json(ROOT/'smoke_manifest.json'); budget=TokenBudget(policy(manifest,1));wires=[]
        with TemporaryDirectory() as tmp,patch('hexgent.client.get_client',side_effect=AssertionError('不得读取凭证')):
            for e in manifest['fixtures'][:2]:
                oracle=read_json(Path(e['oracle_v2_path']))
                summary='<summary>'+ '\n'.join(f['quote'] for f in oracle['facts'] if f['summary_responsibility']=='SUMMARY_REQUIRED')+'</summary>'
                client=make_client(manifest,execute=True,mock_transport=mock_transport(summary,wires))
                try:
                    self.assertEqual(client.max_retries,0)
                    result=execute(e,Path(tmp),budget,client=client,transport_mode='MOCK_TRANSPORT')
                    self.assertEqual(result['status'],'PASS');self.assertEqual(result['actual_network_calls'],0)
                    evidence=read_json(Path(tmp)/e['fixture_id']/'evidence.json')
                    self.assertTrue(all(c['is_real_compaction_call']==(c['call_type']=='COMPACTION') for c in evidence['calls']))
                    self.assertTrue(all(c['model']=='gpt-6.1-sol' for c in evidence['calls']))
                finally:client.close()
        self.assertEqual(len(wires),2);self.assertEqual(budget.calls,2)
        self.assertTrue(all(w['stream'] and w['max_output_tokens']==2000 for w in wires))
    def test_production_retry_new_spans(self):
        manifest=read_json(ROOT/'smoke_manifest.json');b=TokenBudget(policy(manifest,1))
        with TemporaryDirectory() as tmp:
            result=execute(manifest['fixtures'][0],Path(tmp),b,summary_override=['无summary'])
            rows=read_json(Path(tmp)/'F1-02/provider-trace.json')['calls']
        comp=[r for r in rows if r['call_type']=='COMPACTION']
        self.assertEqual(result['compaction_calls'],2)
        self.assertEqual([r['logical_attempt'] for r in comp],[1,2])
        self.assertNotEqual(comp[0]['span_id'],comp[1]['span_id'])


if __name__=='__main__':unittest.main()
