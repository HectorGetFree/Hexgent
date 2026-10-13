"""真实Smoke代理离线透明性检查，不构造真实客户端。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from .real_smoke import Capture,SmokeStop
from eval.context_management.c1_independent import response

class Native:
    def __init__(self,events=None):
        self.responses=self;self.options=[];self.requests=[]
        self.events=events if events is not None else [SimpleNamespace(type='response.completed',response=response('完成'))]
    def with_options(self,**options):self.options.append(options);return self
    def create(self,**request):self.requests.append(request);return iter(self.events)

class CaptureTests(unittest.TestCase):
    def test_options_forward_and_usage_is_provider_value(self):
        with TemporaryDirectory() as root:
            native=Native();proxy=Capture(native,Path(root)).with_options(timeout=9,max_retries=0)
            req=dict(model='fake',instructions='检查',input=[],tools=[],max_output_tokens=10)
            self.assertEqual(list(proxy.responses.create(**req))[0].type,'response.completed')
            self.assertEqual(native.requests,[req]);self.assertEqual(native.options,[dict(timeout=9,max_retries=0)])
            row=json.loads((Path(root)/'provider-ledger.json').read_text())[0]
            self.assertTrue(row['usage_available']);self.assertEqual(row['request'],req)
            with self.assertRaises(SmokeStop):proxy.with_options(max_retries=1)

    def test_incomplete_stream_stops_next_dispatch(self):
        with TemporaryDirectory() as root:
            native=Native([]);proxy=Capture(native,Path(root))
            req=dict(instructions='检查',input=[],tools=[],max_output_tokens=10)
            with self.assertRaises(SmokeStop):list(proxy.create(**req))
            with self.assertRaises(SmokeStop):proxy.create(**req)
            self.assertEqual(len(native.requests),1)
            row=proxy.state['records'][0];self.assertIsNone(row['input_tokens']);self.assertEqual(row['status'],'PROVIDER_STREAM_INTERRUPTED')

    def test_budget_stop_precedes_native_call(self):
        with TemporaryDirectory() as root:
            native=Native();proxy=Capture(native,Path(root))
            from .harness import RequestBudget,BudgetStop
            proxy.state['budget']=RequestBudget(0,200000)
            with self.assertRaises(BudgetStop):proxy.create(instructions='检查',input=[],tools=[],max_output_tokens=10)
            self.assertEqual(native.requests,[])

if __name__=='__main__':unittest.main()
