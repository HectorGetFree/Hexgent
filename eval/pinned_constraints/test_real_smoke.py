"""真实模型 Runner 的离线安全、隔离及独立行为 Oracle 回归。"""
from pathlib import Path
import unittest
from types import SimpleNamespace
from openai.types.responses import ResponseReasoningItem, ResponseFunctionToolCall
from eval.pinned_constraints.real_smoke import RealClient, audit_snapshot
from eval.pinned_constraints.real_smoke import A,B,EXPECTED,protected_request,behavior,availability


class RealSmokeTests(unittest.TestCase):
    def constraints(self,corrected=False):
        rows=[dict(constraint_id="C-A",text=A,source_event_seq=1,status="superseded" if corrected else "active",superseded_by="C-B" if corrected else None)]
        if corrected:
            rows.append(dict(constraint_id="C-B",text=B,source_event_seq=2,status="active",superseded_by=None))
        return rows
    def request(self):
        return dict(model="offline",instructions="system",input=[dict(role="user",content="继续")],tools=[],max_output_tokens=100)
    def test_only_protected_section_differs(self):
        original=self.request()
        proposed,block,_=protected_request(original,self.constraints(),True,10000)
        self.assertEqual({**proposed,"instructions":proposed["instructions"][:-len(block)]},original)
    def test_budget_is_checked_before_dispatch(self):
        with self.assertRaisesRegex(RuntimeError,"BUDGET"):
            protected_request(self.request(),self.constraints(),True,1)
    def test_old_constraint_excluded_after_correction(self):
        _,block,_=protected_request(self.request(),self.constraints(True),True,10000)
        self.assertIn(B,block)
        self.assertNotIn('"C-A"',block)
    def test_prohibited_attempt_counts_even_if_denied(self):
        transcript=[dict(type="tool_execution_started",payload=dict(call_id="x",tool_name="write_file",arguments='{"path":"result.json","content":"{}"}')),
                    dict(type="tool_execution_finished",payload=dict(call_id="x",tool_name="write_file",status="denied"))]
        result=behavior(self.constraints(),transcript,{}, {},"{}",Path("/tmp/work"))
        self.assertFalse(result["constraint_behavior_pass"])
    def test_allowed_result_does_not_hide_source_mutation(self):
        result=behavior(self.constraints(True),[],{"project.md":"old"},{"project.md":"changed"},"{}",Path("/tmp/work"))
        self.assertFalse(result["constraint_behavior_pass"])
    def test_request_availability_distinguishes_channels(self):
        attempt=dict(request={**self.request(),"input":[dict(role="developer",content=A)]},block="")
        result=availability([attempt],A)[0]
        self.assertTrue(result["channels"]["summary"])
        self.assertFalse(result["channels"]["pinned"])

    def test_documents_unset_sdk_status_serialization_gap(self):
        # 最小离线复现：此测试记录缺口，不表示包装透明性通过。
        from openai.types.responses import ResponseReasoningItem
        from eval.pinned_constraints.gate import serial
        item=ResponseReasoningItem(id="rs_offline",summary=[],type="reasoning")
        self.assertNotIn("status",item.to_dict(mode="json"))
        self.assertIn("status",serial(item))
        self.assertIsNone(serial(item)["status"])


class WrapperTransparencyTests(unittest.TestCase):
    constraints=RealSmokeTests.constraints
    request=RealSmokeTests.request
    def native(self, failure=None):
        calls=[]
        options=[]
        def create(**request):
            calls.append(request)
            if failure:
                raise failure
            return iter([])
        client=SimpleNamespace(responses=SimpleNamespace(create=create),calls=calls,options=options)
        def with_options(**kwargs):
            options.append(kwargs)
            return client
        client.with_options=with_options
        return client

    def wrapped(self, native, request, pinned=True, capacity=10000):
        return RealClient(native,[],self.constraints(),pinned,capacity,audit_snapshot(request))

    def test_multiround_sdk_history_identity_and_wire_equivalence(self):
        request=self.request()
        native=self.native()
        client=self.wrapped(native,request)
        list(client.create(**request))
        reasoning=ResponseReasoningItem(id="rs",summary=[],type="reasoning")
        call=ResponseFunctionToolCall(id="fc",call_id="c1",name="read_file",arguments='{}',type="function_call")
        history=[reasoning,call,dict(type="function_call_output",call_id="c1",output="actual content")]
        second={**request,"input":history}
        list(client.create(**second))
        sent=native.calls[-1]
        self.assertIs(sent["input"],history)
        self.assertIs(sent["input"][0],reasoning)
        self.assertIs(sent["input"][1],call)
        self.assertNotIn("status",client.records[-1]["request"]["input"][0])
        snapshot=audit_snapshot(sent)
        snapshot["instructions"]=request["instructions"]
        self.assertEqual(snapshot,audit_snapshot(second))

    def test_baseline_is_transparent_and_audit_is_independent(self):
        request=self.request()
        native=self.native()
        client=self.wrapped(native,request,pinned=False)
        list(client.create(**request))
        self.assertIs(native.calls[0]["input"],request["input"])
        self.assertEqual(native.calls[0],request)
        request["input"][0]["content"]="changed"
        self.assertEqual(client.records[0]["request"]["input"][0]["content"],"继续")

    def test_explicit_null_is_not_removed(self):
        item=ResponseReasoningItem(id="rs",summary=[],type="reasoning",status=None)
        self.assertIn("status",audit_snapshot(item))

    def test_options_share_observation_and_forward_unchanged(self):
        request=self.request()
        native=self.native()
        client=self.wrapped(native,request)
        changed=client.with_options(timeout=19,max_retries=0)
        list(changed.create(**request))
        self.assertEqual(native.options,[dict(timeout=19,max_retries=0)])
        self.assertIs(changed.records,client.records)
        self.assertIs(changed.attempts,client.attempts)

    def test_transport_error_propagates_same_exception(self):
        error=TimeoutError("offline timeout")
        request=self.request()
        native=self.native(error)
        client=self.wrapped(native,request)
        with self.assertRaises(TimeoutError) as caught:
            client.create(**request)
        self.assertIs(caught.exception,error)
        self.assertEqual(client.records[0]["transport_error"]["type"],"TimeoutError")

    def test_budget_and_isolation_block_before_native_dispatch(self):
        request=self.request()
        for capacity,expected in [(1,audit_snapshot(request)),(10000,{})]:
            native=self.native()
            client=RealClient(native,[],self.constraints(),True,capacity,expected)
            with self.assertRaises(RuntimeError):
                client.create(**request)
            self.assertEqual(native.calls,[])

    def test_stream_failure_and_close_forward(self):
        error=ConnectionError("offline interrupted")
        class Stream:
            closed=False
            def __iter__(self): return self
            def __next__(self): raise error
            def close(self): self.closed=True
        stream=Stream()
        request=self.request()
        native=self.native()
        native.responses.create=lambda **kwargs:stream
        client=self.wrapped(native,request)
        wrapped=client.create(**request)
        with self.assertRaises(ConnectionError) as caught:
            next(wrapped)
        self.assertIs(caught.exception,error)
        wrapped.close()
        self.assertTrue(stream.closed)
        self.assertEqual(client.records[0]["transport_error"]["type"],"ConnectionError")


if __name__=="__main__":
    unittest.main()
