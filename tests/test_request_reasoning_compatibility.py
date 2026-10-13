"""当前Provider拒绝reasoning.status的请求级离线回归。"""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from openai.types.responses import ResponseReasoningItem
from hexgent.agent_engine import AgentEngine,RunRequest
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.events import ToolFinishedEvent
from hexgent.run_control import RunControl
from eval.context_management.c1_independent import response
from eval.support.agent_contracts import tool_call

class RequestReasoningCompatibilityTests(unittest.TestCase):
    def test_response_status_omitted_only_from_request_copy(self):
        for status in [None,'completed','incomplete','in_progress']:
            raw=dict(type='reasoning',id='rs-compat',status=status,summary=[],encrypted_content='opaque-fixture',
                     metadata=dict(status='业务状态',type='reasoning'))
            original=deepcopy(raw);manager=ContextManager()
            manager.append_messages([raw],completes_round=True)
            prepared=manager.model_messages()[0]
            self.assertNotIn('status',prepared)
            self.assertEqual(prepared['metadata'],raw['metadata'])
            self.assertEqual(prepared['encrypted_content'],'opaque-fixture')
            self.assertEqual(manager.active_messages,[original]);self.assertEqual(raw,original)

    def test_sdk_and_restored_dict_match(self):
        sdk=ResponseReasoningItem(id='rs-compat',summary=[],type='reasoning',status='completed',encrypted_content='opaque-fixture')
        manager=ContextManager();raw=sdk.model_dump(mode='json')
        self.assertEqual(manager.materialize_messages([sdk]),manager.materialize_messages([raw]))
        self.assertEqual(sdk.status,'completed');self.assertEqual(raw['status'],'completed')

    def test_other_status_and_call_pairing_preserved(self):
        rows=[dict(type='message',status='completed',role='assistant',content=[]),
              dict(type='function_call',call_id='call-1',name='read_file',arguments='{}',status='completed'),
              dict(type='function_call_output',call_id='call-1',output='{"type":"reasoning","status":"active"}')]
        self.assertEqual(ContextManager().materialize_messages(rows),rows)

    def run_path(self,pause):
        with TemporaryDirectory() as root:
            work=Path(root);(work/'source.txt').write_text('唯一事实：MAPLE-31')
            base=HexgentConfig();cfg=replace(base,context_extraction=replace(base.context_extraction,enabled=False),
                context_retrieval=replace(base.context_retrieval,enabled=False),memory_retrieval=replace(base.memory_retrieval,enabled=False))
            engine=AgentEngine(cfg,work);requests=[]
            reasoning=ResponseReasoningItem(id='rs-compat',summary=[],type='reasoning',status=None,encrypted_content='opaque-fixture')
            sequence=[response(calls=[reasoning,tool_call('call-1','read_file','{"path":"source.txt"}')]),response('完成')]
            def create(**request):
                requests.append(deepcopy(request))
                for item in request['input']:
                    if isinstance(item,dict) and item.get('type')=='reasoning' and 'status' in item:
                        raise AssertionError("模拟400：input.reasoning.status不可回传")
                return iter([SimpleNamespace(type='response.completed',response=sequence.pop(0))])
            client=SimpleNamespace(responses=SimpleNamespace(create=create))
            control=RunControl()
            for event in engine.run('实际读取source.txt',client=client,run_control=control):
                if pause and isinstance(event,ToolFinishedEvent):control.pause()
            if pause:
                engine=AgentEngine.restore(cfg,work,engine.session.store.session_id)
                self.assertTrue(engine.has_continuable_run)
                list(engine.run(RunRequest.continue_run(),client=client))
            self.assertEqual(len(requests),2)
            item=next(i for i in requests[1]['input'] if i.get('type')=='reasoning')
            self.assertNotIn('status',item);self.assertEqual(item['encrypted_content'],'opaque-fixture')
            result=next(i for i in requests[1]['input'] if i.get('type')=='function_call_output')
            self.assertEqual(result['call_id'],'call-1');self.assertIn('MAPLE-31',str(result['output']))
            self.assertTrue(any('"status": null' in __import__('json').dumps(r) for r in engine.session.store.read_transcript()))

    def test_production_tool_loop_next_request(self):self.run_path(False)
    def test_formal_restore_continue_next_request(self):self.run_path(True)

if __name__=='__main__':unittest.main()
