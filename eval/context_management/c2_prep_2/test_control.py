"""离线故障注入；测试费率仅为算术 Fixture，不是 Provider 报价。"""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import json
import httpx
from openai import OpenAI

from .control import Budget, BudgetDenied, Trace
from .runner import fake_policy, run_fixture
from eval.context_management.c2_prep.prepare import assets, StreamRecorder
from eval.context_management.c2_prep.catalog import CASES
from eval.context_management.c1_independent import Gate, response, save


class BudgetTests(TestCase):
    def setUp(self):
        self.p=replace(fake_policy(),max_provider_calls=2,max_input_tokens_per_call=100,
            max_output_tokens_per_call=10,max_total_input_tokens=150,max_total_output_tokens=15)
        self.req={'max_output_tokens':5}
    def budget(self,**kw):
        return Budget(replace(self.p,**kw),lambda r:r.get('bound',50))
    def test_normal(self):
        b=self.budget(); b.reserve(self.req); self.assertEqual(b.calls,1)
    def test_input_limit(self):
        with self.assertRaisesRegex(BudgetDenied,'PER_CALL_INPUT'):
            self.budget().reserve(dict(self.req,bound=101))
    def test_output_limit(self):
        with self.assertRaisesRegex(BudgetDenied,'PER_CALL_OUTPUT'):
            self.budget().reserve({'max_output_tokens':11})
    def test_total_input(self):
        b=self.budget(max_total_input_tokens=60); b.reserve(self.req)
        with self.assertRaisesRegex(BudgetDenied,'TOTAL_INPUT'): b.reserve(self.req)
    def test_total_output(self):
        b=self.budget(max_total_output_tokens=7); b.reserve(self.req)
        with self.assertRaisesRegex(BudgetDenied,'TOTAL_OUTPUT'): b.reserve(self.req)
    def test_total_cost(self):
        with self.assertRaisesRegex(BudgetDenied,'TOTAL_COST'):
            self.budget(max_estimated_cost='0.00001').reserve(self.req)
    def test_retry_exhaustion_and_zero_extra(self):
        b=self.budget(); b.reserve(self.req); b.reserve(self.req)
        with self.assertRaisesRegex(BudgetDenied,'PROVIDER_CALL'): b.reserve(self.req)
        self.assertEqual(b.calls,2)
    def test_inflight_reservation(self):
        b=self.budget(max_provider_calls=1); row=b.reserve(self.req)
        self.assertEqual(row['status'],'IN_FLIGHT')
        with self.assertRaises(BudgetDenied): b.reserve(self.req)
    def test_unknown_price_and_input_bounds(self):
        with self.assertRaisesRegex(BudgetDenied,'UNCONFIRMED'):
            self.budget(confirmed=False).reserve(self.req)
        with self.assertRaisesRegex(BudgetDenied,'INPUT_TOKEN_BOUND_UNKNOWN'):
            Budget(self.p).reserve(self.req)
    def test_negative_price_rejected(self):
        with self.assertRaisesRegex(BudgetDenied,'INVALID_PRICE'):
            self.budget(input_price_per_million='-1').reserve(self.req)
    def test_concurrent_inflight_reservation(self):
        from concurrent.futures import ThreadPoolExecutor
        b=self.budget(max_provider_calls=1)
        def attempt(_):
            try: b.reserve(self.req); return True
            except BudgetDenied: return False
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(sum(pool.map(attempt,range(4))),1)


class TraceTests(TestCase):
    def make(self,path,delegate,policy=None):
        return Trace(delegate,path,Budget(policy or fake_policy(),lambda r:50),eval_run_id='offline-test',fixture_id='FAULT')
    def test_missing_usage_and_failure_conservative(self):
        with TemporaryDirectory() as tmp:
            native=SimpleNamespace(responses=SimpleNamespace(create=lambda **r:(_ for _ in ()).throw(TimeoutError('offline timeout'))),base_url='offline://fake',max_retries=0)
            trace=self.make(Path(tmp)/'trace.json',native)
            with self.assertRaises(TimeoutError): trace.responses.create(max_output_tokens=5)
            row=trace.state['calls'][0]
            self.assertEqual(row['outcome'],'TIMEOUT'); self.assertIsNone(row['input_tokens'])
            self.assertEqual(row['reservation']['status'],'CONSERVATIVELY_CHARGED')
            self.assertEqual(trace.budget.calls,1)
    def test_dispatch_reject_zero_native_calls(self):
        with TemporaryDirectory() as tmp:
            calls=[]
            native=SimpleNamespace(responses=SimpleNamespace(create=lambda **r:calls.append(r)))
            trace=self.make(Path(tmp)/'trace.json',native,replace(fake_policy(),max_provider_calls=0))
            with self.assertRaises(BudgetDenied): trace.responses.create(max_output_tokens=5)
            self.assertEqual(calls,[]); self.assertEqual(trace.state['calls'],[])
    def test_stream_missing_usage_and_cancel(self):
        with TemporaryDirectory() as tmp:
            def stream(**r):
                value=response('完成'); value.usage=None
                yield SimpleNamespace(type='response.completed',response=value)
            trace=self.make(Path(tmp)/'trace.json',SimpleNamespace(responses=SimpleNamespace(create=stream)))
            list(trace.responses.create(stream=True,max_output_tokens=5))
            self.assertEqual(trace.state['calls'][0]['usage_status'],'MISSING')
            value=trace.responses.create(stream=True,max_output_tokens=5); value.close()
            self.assertEqual(trace.state['calls'][1]['outcome'],'CANCELLED')
    def test_real_profile_missing_usage_stops_next_dispatch(self):
        with TemporaryDirectory() as tmp:
            calls=[]
            def create(**r):
                calls.append(r); value=response('完成'); value.usage=None
                return value
            trace=self.make(Path(tmp)/'trace.json',SimpleNamespace(responses=SimpleNamespace(create=create),max_retries=0),replace(fake_policy(),dry_only=False))
            trace.responses.create(max_output_tokens=5)
            with self.assertRaisesRegex(BudgetDenied,'COST_OBSERVABILITY_BLOCKED'): trace.responses.create(max_output_tokens=5)
            self.assertEqual(len(calls),1)
    def test_sdk_physical_retry_zero_local_transport(self):
        wires=[]
        def handler(request):
            wires.append(request)
            return httpx.Response(500,json={'error':{'message':'offline fault'}})
        sdk=OpenAI(api_key='OFFLINE-NOT-A-SECRET',base_url='https://offline.invalid/v1',max_retries=2,
                   http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        try:
            with TemporaryDirectory() as tmp:
                trace=self.make(Path(tmp)/'trace.json',sdk,replace(fake_policy(),dry_only=False))
                with self.assertRaisesRegex(BudgetDenied,'SDK_RETRY'): trace.responses.create(model='fake',input='x',max_output_tokens=5)
                self.assertEqual(len(wires),0)
                trace=trace.with_options(max_retries=0)
                with self.assertRaises(Exception): trace.responses.create(model='fake',input='x',max_output_tokens=5)
                self.assertEqual(len(wires),1); self.assertEqual(len(trace.state['calls']),1)
        finally:
            sdk.close()


class ProductionTraceTests(TestCase):
    def execute(self,summary):
        with TemporaryDirectory() as tmp:
            output=Path(tmp)
            fixture,facts=assets(CASES[0]); rounds=[]
            from eval.context_management.c1_independent import plain
            for m in fixture['messages']:
                rounds.append(dict(query=m['text'],messages=plain(response('已核对').output)))
            original=StreamRecorder.create
            def fake(obj,**request):
                from hexgent.context.context_compaction import COMPACTION_INSTRUCTIONS
                if request['instructions'].startswith(COMPACTION_INSTRUCTIONS):
                    if summary:
                        value=summary.pop(0)
                        if isinstance(value,Exception): raise value
                        obj.summary=[value]
                yield from original(obj,**request)
            with patch.object(StreamRecorder,'create',fake):
                result=run_fixture('F-FAULT',rounds,facts,output,Gate(output).config(),'继续')
            self.assertTrue(result['attribution']['pass_'])
            self.assertEqual(result['compaction_calls'],2)
            data=json.loads((output/'F-FAULT'/'provider-trace.json').read_text())
            retries=[r['retry_index'] for r in data['calls'] if r['call_type']=='COMPACTION']
            self.assertEqual(retries,[0,1])
            self.assertTrue(all(r['agent_run_id']!='NOT_AVAILABLE' and r['span_id']!='NOT_AVAILABLE' for r in data['calls']))
    def test_format_retry_then_success(self):
        self.execute(['无summary格式'])
    def test_provider_error_then_success(self):
        self.execute([RuntimeError('offline provider error')])
    def test_timeout_then_success(self):
        self.execute([TimeoutError('offline timeout')])
    def test_production_budget_zero_dispatch(self):
        from hexgent.agent_engine import AgentEngine
        with TemporaryDirectory() as tmp:
            root=Path(tmp); cfg=Gate(root).config()
            cfg=replace(cfg,loop=replace(cfg.loop,max_output_tokens_per_round=200000))
            engine=AgentEngine(cfg,root/'workspace')
            delegate=SimpleNamespace(responses=StreamRecorder({'requests':[]}),max_retries=0)
            trace=Trace(delegate,root/'trace.json',Budget(fake_policy(),lambda r:50),eval_run_id='budget',fixture_id='ZERO',session=engine.session)
            with patch('hexgent.loop.agent_turn.get_model',return_value='FAKE_ONLY'):
                events=list(engine.run('继续',client=trace))
            self.assertEqual(trace.state['calls'],[])
            self.assertIn('CONTEXT_BUDGET_EXCEEDED',events[-1].result.output_text)
