"""M3 真实请求观测包装的离线契约；不调用 Provider。"""
from types import SimpleNamespace
import unittest

from eval.memory_m3.real_smoke import CaptureClient, CaptureStream, blocks
from eval.memory_m3.audit_smoke import correlate


class CaptureTests(unittest.TestCase):
    def test_usage_and_failure_type_come_from_matching_start_span(self):
        events=[dict(event="provider_started",span_id="x",request_type="CONTEXT_RECORD_EXTRACTION"),
                dict(event="provider_finished",span_id="x",status="INTERRUPTED",input_tokens=None,output_tokens=None,usage_available=False)]
        result=correlate(events)[0]
        self.assertEqual(result["request_type"],"CONTEXT_RECORD_EXTRACTION")
        self.assertIsNone(result["input_tokens"])
        self.assertEqual(result["status"],"INTERRUPTED")

    def test_unfinished_provider_span_is_not_dropped(self):
        result=correlate([dict(event="provider_started",span_id="x",request_type="MAIN_AGENT")])
        self.assertEqual(len(result),1)
        self.assertFalse(result[0]["finished_observed"])

    def test_computed_output_text_is_saved_without_replacing_event(self):
        response = SimpleNamespace(output_text='{"records":[]}', model_dump=lambda **k: {"id": "response-1"})
        event = SimpleNamespace(type="response.completed", response=response)
        recorded = {}
        stream = CaptureStream(iter([event]), recorded)
        self.assertIs(next(stream), event)
        self.assertEqual(recorded["completed_response"]["output_text"], response.output_text)
        with self.assertRaises(StopIteration):
            next(stream)

    def test_options_and_create_preserve_native_arguments(self):
        received = []
        event = SimpleNamespace(type="response.output_text.delta", delta="abc")
        class Native:
            def __init__(self):
                self.responses = SimpleNamespace(create=self.create)
            def with_options(self, **options):
                received.append(options)
                return self
            def create(self, **request):
                received.append(request)
                return iter([event])
        records = []
        client = CaptureClient(Native(), records).with_options(timeout=8, max_retries=0)
        request = dict(model="offline", stream=True, tools=[], input=[])
        self.assertIs(next(client.responses.create(**request)), event)
        self.assertEqual(received, [dict(timeout=8, max_retries=0), request])
        self.assertEqual(records[0]["request"], request)

    def test_record_block_is_separate_from_history(self):
        recorded = [{"request": {"instructions": '<relevant-session-context>\n[{"record_id":"r1","content":"new"}]\n</relevant-session-context>', "input": "old"}}]
        self.assertEqual(blocks(recorded), [dict(record_id="r1", content="new")])
        self.assertEqual(blocks([{"request": {"instructions": "summary: old"}}]), [])

    def test_native_error_is_not_suppressed(self):
        class Broken:
            def __iter__(self):
                return self
            def __next__(self):
                raise TimeoutError("offline timeout")
        with self.assertRaises(TimeoutError):
            next(CaptureStream(Broken(), {}))


if __name__ == "__main__":
    unittest.main()
