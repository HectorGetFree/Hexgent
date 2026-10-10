"""Eval 代理透明性和分层 Oracle 回归；不调用网络。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from .v3_provider import Observer, usage_report
from .v3_oracles import layered_oracles


class Payload:
    def __init__(self, **value):
        self.__dict__.update(value)

    def model_dump(self, **kwargs):
        return self.__dict__


class Stream:
    def __init__(self, events=()):
        self.events = iter(events)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.events)

    def close(self):
        self.closed = True


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = Mock(base_url="https://provider.invalid")
        self.observer = Observer(self.client, Path(self.temp.name) / "calls.json")

    def terminal(self, usage=None, kind="response.completed"):
        return SimpleNamespace(type=kind, response=Payload(id="r1", output=[], usage=usage, error=None))

    def test_failed_response_null_output_preserves_provider_error(self):
        error = {'code': 'server_error', 'message': 'Service temporarily unavailable, please retry later.'}
        event = SimpleNamespace(type='response.failed', response=Payload(id='failed-id', output=None, usage=None, error=error))
        self.client.responses.create.return_value = Stream([event])
        stream = self.observer.responses.create(model='m', stream=True)
        self.assertIs(next(stream), event)
        stream.close()
        row = self.observer.turns[0]
        self.assertEqual(row['status'], 'PROVIDER_FAILED')
        self.assertEqual(row['failure_reason'], error)
        self.assertEqual(row['tool_calls'], [])
        self.assertIsNone(row['input_tokens'])

    def test_options_forwarded_and_shared_auxiliary_observation(self):
        from hexgent.memory.memory_prompts import CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS
        scoped = Mock(base_url="https://provider.invalid")
        self.client.with_options.return_value = scoped
        scoped.responses.create.return_value = Stream([self.terminal({"input_tokens": 4, "output_tokens": 2})])
        child = self.observer.with_options(timeout=8, max_retries=0)
        list(child.responses.create(model="m", instructions=CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS, stream=True))
        self.client.with_options.assert_called_once_with(timeout=8, max_retries=0)
        self.assertIs(child.turns, self.observer.turns)
        self.assertEqual(usage_report(self.observer.turns)["auxiliary"]["observable_input_tokens"], 4)

    def test_eager_create_exact_parameters_and_event_identity(self):
        event = self.terminal({"input_tokens": 3, "output_tokens": 1})
        self.client.responses.create.return_value = Stream([event])
        request = {"model": "m", "stream": True, "input": [{"role": "user", "content": "original"}]}
        stream = self.observer.responses.create(**request)
        self.client.responses.create.assert_called_once_with(**request)
        request["input"][0]["content"] = "changed"
        self.assertIs(next(stream), event)
        self.assertEqual(self.observer.turns[0]["request"]["input"][0]["content"], "original")
        stream.close()
        self.assertEqual(self.observer.turns[0]["status"], "SUCCEEDED")

    def test_create_exception_identity_preserved(self):
        error = TimeoutError("timeout")
        self.client.responses.create.side_effect = error
        with self.assertRaises(TimeoutError) as caught:
            self.observer.responses.create(stream=True)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.observer.turns[0]["status"], "TIMEOUT")
        self.assertIsNone(self.observer.turns[0]["input_tokens"])

    def test_iteration_exception_identity_preserved(self):
        error = RuntimeError("stream interrupted")
        raw = Mock()
        iter_ = Mock()
        raw.__iter__ = Mock(return_value=iter_)
        iter_.__next__ = Mock(side_effect=error)
        self.client.responses.create.return_value = raw
        stream = self.observer.responses.create(stream=True)
        with self.assertRaises(RuntimeError) as caught:
            next(stream)
        self.assertIs(caught.exception, error)
        stream.close()

    def test_cross_thread_close_reaches_underlying_stream(self):
        raw = Stream()
        self.client.responses.create.return_value = raw
        stream = self.observer.responses.create(stream=True)
        thread = threading.Thread(target=stream.close)
        thread.start()
        thread.join(1)
        self.assertTrue(raw.closed)
        self.assertEqual(self.observer.turns[0]["status"], "CANCELLED")
        stream.close()

    def test_missing_usage_not_zero(self):
        self.client.responses.create.return_value = Stream([self.terminal()])
        list(self.observer.responses.create(stream=True))
        report = usage_report(self.observer.turns)["all"]
        self.assertEqual(report["unobservable_requests"], 1)
        self.assertIsNone(report["observable_input_tokens"])

    def test_close_unblocks_inflight_iteration_without_generator_error(self):
        entered, released = threading.Event(), threading.Event()
        class Blocking(Stream):
            def __next__(self):
                entered.set()
                released.wait(1)
                raise StopIteration

            def close(self):
                super().close()
                released.set()
        raw = Blocking()
        self.client.responses.create.return_value = raw
        stream = self.observer.responses.create(stream=True)
        thread = threading.Thread(target=lambda: list(stream))
        thread.start()
        self.assertTrue(entered.wait(1))
        stream.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(raw.closed)

    def test_context_manager_propagates_suppression_and_closes(self):
        raw = Mock()
        raw.__iter__ = Mock(return_value=iter([]))
        raw.__enter__ = Mock(return_value=raw)
        raw.__exit__ = Mock(return_value=True)
        self.client.responses.create.return_value = raw
        with self.observer.responses.create(stream=True):
            raise ValueError("suppressed by delegate")
        raw.__exit__.assert_called_once()
        self.assertEqual(self.observer.turns[0]["status"], "CANCELLED")

    def test_nonstream_returns_original_response(self):
        response = Payload(id="r", status="completed", output=[], usage={"input_tokens": 0, "output_tokens": 0})
        self.client.responses.create.return_value = response
        self.assertIs(self.observer.responses.create(stream=False), response)
        self.assertTrue(self.observer.turns[0]["usage_available"])

    def test_failure_event_not_success(self):
        event = self.terminal(kind="response.failed")
        self.client.responses.create.return_value = Stream([event])
        self.assertIs(list(self.observer.responses.create(stream=True))[0], event)
        self.assertEqual(self.observer.turns[0]["status"], "PROVIDER_FAILED")

    def test_observation_io_does_not_mask_provider_error(self):
        self.observer.destination = Path(self.temp.name) / "missing" / "calls.json"
        error = ValueError("original")
        self.client.responses.create.side_effect = error
        with self.assertRaises(ValueError) as caught:
            self.observer.responses.create()
        self.assertIs(caught.exception, error)
        self.assertTrue(self.observer.state["observation_errors"])

    def test_oracles_separate_bad_quotes_from_tool_delivery(self):
        records = [
            {"type": "permission_resolved", "payload": {"call_id": "c", "behavior": "allow"}},
            {"type": "tool_execution_dispatched", "payload": {"call_id": "c"}},
            {"type": "tool_execution_finished", "payload": {"call_id": "c", "status": "success"}},
            {"type": "context_messages_appended", "payload": {"messages": [{"type": "function_call_output", "call_id": "c", "output": "truth"}]}}]
        turns = [{"request_type": "MAIN_AGENT", "tool_calls": [{"call_id": "c"}], "request": {}},
                 {"request_type": "MAIN_AGENT", "tool_calls": [], "request": {"input": [{"type": "function_call_output", "call_id": "c", "output": "truth"}]}}]
        result = layered_oracles(records, turns, {"mechanical_pass": False})
        self.assertEqual([result[k]["status"] for k in ("tool_execution", "tool_result_delivery", "task_evidence")], ["PASS", "PASS", "FAIL"])
        turns[1]["request"]["input"][0]["output"] = "wrong"
        self.assertEqual(layered_oracles(records, turns, {"mechanical_pass": True})["tool_result_delivery"]["status"], "FAIL")

    def test_strict_quote_checks_each_reference_without_relaxation(self):
        import json
        from .v3_smoke import oracle
        root = Path(self.temp.name)
        (root / "source.py").write_text("exact source content with indentation")
        records = [{"type": "tool_execution_started", "payload": {"call_id": "c", "tool_name": "read_file", "arguments": {"path": "source.py"}}},
                   {"type": "tool_execution_finished", "payload": {"call_id": "c", "status": "success"}}]
        answer = json.dumps({"findings": [{"conclusion": "claim", "evidence": [
            {"path": "source.py", "quote": "exact source content"},
            {"path": "source.py", "quote": "exact ... content"}]}]})
        result = oracle({"questions": ["q"], "required": ["source.py"]}, records, answer, root)
        self.assertFalse(result["mechanical_pass"])
        self.assertEqual([q["pass"] for q in result["quote_checks"]], [True, False])
