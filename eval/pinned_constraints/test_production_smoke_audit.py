"""透明代理与中断持久化离线 Gate，不调用网络。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from eval.pinned_constraints.production_real_smoke import AuditClient


class AuditTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "requests.json"
        self.records = []

    def test_request_persisted_before_dispatch_and_interrupt(self):
        def create(**request):
            self.assertEqual(json.loads(self.path.read_text())[0]["request"], request)
            raise KeyboardInterrupt()
        client = AuditClient(SimpleNamespace(responses=SimpleNamespace(create=create)), self.records, self.path)
        with self.assertRaises(KeyboardInterrupt):
            client.create(model="offline", input="test")
        self.assertEqual(json.loads(self.path.read_text())[0]["status"], "INTERRUPTED")

    def test_completed_response_and_usage_durable(self):
        class Response:
            output_text = "ok"
            def model_dump(self, **kwargs):
                return dict(output_text="ok", usage=dict(input_tokens=3, output_tokens=1))
        event = SimpleNamespace(type="response.completed", response=Response())
        native = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: iter([event])))
        client = AuditClient(native, self.records, self.path)
        self.assertIs(next(client.create(input="x")), event)
        record = json.loads(self.path.read_text())[0]
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["completed_response"]["usage"]["input_tokens"], 3)

    def test_midstream_interrupt_keeps_partial_events(self):
        def events():
            yield SimpleNamespace(type="response.output_text.delta", delta="partial")
            raise KeyboardInterrupt()
        native = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: events()))
        stream = AuditClient(native, self.records, self.path).create(input="x")
        next(stream)
        with self.assertRaises(KeyboardInterrupt): next(stream)
        record = json.loads(self.path.read_text())[0]
        self.assertEqual(record["status"], "INTERRUPTED")
        self.assertEqual(len(record["stream_events"]), 1)

    def test_options_and_original_request_are_forwarded(self):
        marker = object()
        seen = []
        native = SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: seen.append(kw) or iter([])))
        native.with_options = lambda **kw: seen.append(kw) or native
        client = AuditClient(native, self.records, self.path).with_options(timeout=12)
        list(client.create(input=marker))
        self.assertEqual(seen[0], dict(timeout=12))
        self.assertIs(seen[1]["input"], marker)
        self.assertEqual(json.loads(self.path.read_text())[0]["status"], "STREAM_ENDED_WITHOUT_TERMINAL")
