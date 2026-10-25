"""请求级纠偏验收：使用真实 Engine、请求组装与确定性跨线程同步，不访问 Provider。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.loop.limits import LoopLimits
from hexgent.loop.call_model import ModelCallInterrupted, call_model
from hexgent.run_control import RunControl
from hexgent.tool.tool_registry import TOOL_REGISTRY


def completed(text="完成", *, output=None):
    return SimpleNamespace(type="response.completed", response=SimpleNamespace(
        output=output or [], output_text=text,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5)))


class GatedStream:
    """在可控读取点等待关闭；Event 超时仅用于避免测试死锁。"""

    def __init__(self, initial=()):
        self.initial = iter(initial)
        self.waiting = Event()
        self.released = Event()
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        try:
            return next(self.initial)
        except StopIteration:
            self.waiting.set()
            if not self.released.wait(5):
                raise AssertionError("测试流未收到关闭或释放信号")
            raise StopIteration

    def close(self):
        self.closed = True
        self.released.set()


class Client:
    def __init__(self, streams):
        self.streams = iter(streams)
        self.calls = []
        self.responses = self

    def create(self, **request):
        self.calls.append(request)
        return iter(next(self.streams))


class EmptyTransportFailure:
    def __iter__(self):
        return self

    def __next__(self):
        raise RuntimeError("peer closed connection without sending complete message body")


class InterruptingSteerTests(unittest.TestCase):
    def run_gated(self, initial=(), action=None, *, config=None, prefix=()):
        with TemporaryDirectory() as directory:
            engine = AgentEngine.create(config or HexgentConfig(), Path(directory))
            control = RunControl()
            stream = GatedStream(initial)
            client = Client([*prefix, stream, [completed()]])
            with patch("hexgent.loop.agent_turn.get_model", return_value="offline"), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: list(engine.run("原始任务", client=client, run_control=control)))
                try:
                    self.assertTrue(stream.waiting.wait(5))
                    (action or (lambda c: c.steer("先检查风险", mode="interrupt")))(control)
                finally:
                    stream.released.set()
                events = future.result(timeout=5)
            records = engine.session.store.read_transcript()
            return client.calls, records, events[-1].result, stream, engine.context_manager.model_messages()

    def test_generation_yields_same_run_and_next_request_contains_query(self):
        calls, records, result, stream, messages = self.run_gated([
            SimpleNamespace(type="response.output_text.delta", delta="未完成草稿")])
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        for field in ("model", "tools", "max_output_tokens", "instructions"):
            self.assertEqual(calls[0].get(field), calls[1].get(field))
        self.assertTrue(stream.closed)
        self.assertIn("先检查风险", str(calls[1]["input"]))
        self.assertNotIn("未完成草稿", str(calls[1]["input"]))
        self.assertEqual(sum(r["type"] == "agent_run_started" for r in records), 1)
        self.assertEqual(sum(r["type"] == "steering_applied" for r in records), 1)
        yielded = next(r["payload"] for r in records if r["type"] == "model_request_yielded")
        self.assertIsNone(yielded["input_tokens"])
        self.assertIsNone(yielded["output_tokens"])
        self.assertFalse(yielded["usage_available"])
        final = next(r["payload"]["run_state"] for r in reversed(records) if "run_state" in r["payload"])
        self.assertEqual(final["model_rounds"], 2)
        self.assertEqual((final["input_tokens"], final["output_tokens"]), (10, 5))
        self.assertFalse(any(r["type"] == "tool_execution_dispatched" for r in records))

    def test_completed_response_race_preserves_usage_but_does_not_dispatch_tools(self):
        tool = SimpleNamespace(type="function_call", name="write_file", call_id="discarded",
                               arguments='{"path":"forbidden.txt","content":"old"}')
        calls, records, result, _, _ = self.run_gated([completed(output=[tool])])
        self.assertEqual(result.status, "completed")
        self.assertNotIn("discarded", str(calls[1]["input"]))
        self.assertFalse(any(r["type"] == "tool_execution_dispatched" for r in records))
        yielded = next(r["payload"] for r in records if r["type"] == "model_request_yielded")
        self.assertTrue(yielded["completed_response_discarded"])
        self.assertEqual((yielded["input_tokens"], yielded["output_tokens"]), (10, 5))

    def test_prior_empty_stream_retry_is_counted_and_never_reported_complete_usage(self):
        calls, records, result, _, _ = self.run_gated([completed()], prefix=[EmptyTransportFailure()])
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 3)
        yielded = next(r["payload"] for r in records if r["type"] == "model_request_yielded")
        self.assertEqual(yielded["request_attempts"], 2)
        self.assertEqual(yielded["missing_usage_attempts"], 1)
        self.assertFalse(yielded["usage_available"])
        final = next(r["payload"]["run_state"] for r in reversed(records) if "run_state" in r["payload"])
        self.assertEqual(final["model_rounds"], 3)
        self.assertEqual((final["input_tokens"], final["output_tokens"]), (20, 10))

    def test_terminal_controls_win_and_do_not_apply_queued_query(self):
        for terminal, expected in (("pause", "interrupted"), ("cancel", "cancelled")):
            with self.subTest(terminal=terminal):
                def action(control):
                    getattr(control, terminal)()
                    control.steer("不应自动继续", mode="interrupt")
                calls, records, result, _, _ = self.run_gated(action=action)
                self.assertEqual(result.status, expected)
                self.assertEqual(len(calls), 1)
                self.assertFalse(any(r["type"] == "steering_applied" for r in records))

    def test_fifo_does_not_interrupt_run_or_reset_request_controller(self):
        control = RunControl()
        with control.model_request() as first:
            control.steer("第一条", mode="interrupt")
            control.steer("第二条")
            self.assertTrue(first.interrupted)
            self.assertFalse(control.interrupt_controller.interrupted)
        self.assertEqual([q.content for q in control.steering_mailbox.drain()], ["第一条", "第二条"])
        with control.model_request() as second:
            self.assertFalse(second.interrupted)
            control.pause()
            self.assertTrue(second.interrupted)

    def test_boundary_default_does_not_yield_model_request(self):
        control = RunControl()
        with control.model_request() as request:
            control.steer("边界纠偏")
            self.assertFalse(request.interrupted)
        self.assertTrue(control.steering_mailbox.has_pending)

    def test_input_between_requests_is_not_lost(self):
        control = RunControl()
        control.steer("请求间隙到达", mode="interrupt")
        with control.model_request() as request:
            self.assertTrue(request.interrupted)
        self.assertEqual(control.steering_mailbox.drain()[0].content, "请求间隙到达")

    def test_prepare_time_steer_prevents_provider_create(self):
        control = RunControl()
        client = Client([])
        def prepare(request):
            control.steer("准备阶段纠偏", mode="interrupt")
            return request
        with control.model_request() as request:
            with self.assertRaises(ModelCallInterrupted) as caught:
                list(call_model(client, request, prepare_request=prepare))
        self.assertEqual(client.calls, [])
        self.assertEqual(caught.exception.request_attempts, 0)

    def test_finalization_is_not_repeatedly_preempted(self):
        control = RunControl()
        with control.model_request(steerable=False) as request:
            control.steer("不能重置收尾预算", mode="interrupt")
            self.assertFalse(request.interrupted)
            control.cancel()
            self.assertTrue(request.interrupted)

    def test_tool_phase_interrupt_mode_keeps_existing_boundary_semantics(self):
        with TemporaryDirectory() as directory:
            control = RunControl()
            entered = []
            original_read = TOOL_REGISTRY["read_file"].function
            (Path(directory) / "seed.txt").write_text("资料正文", encoding="utf-8")
            def read(path, context):
                entered.append(path)
                result = original_read(path, context)
                control.steer("读取后先分析", mode="interrupt")
                return result
            tool = SimpleNamespace(type="function_call", name="read_file", call_id="read-1",
                                   arguments='{"path":"seed.txt"}')
            client = Client([[completed(output=[tool])], [completed()]])
            engine = AgentEngine.create(HexgentConfig(), Path(directory))
            with patch.dict(TOOL_REGISTRY, {"read_file": replace(TOOL_REGISTRY["read_file"], function=read)}), \
                    patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
                events = list(engine.run("读取资料", client=client, run_control=control,
                                         user_confirmation=lambda *_: "allow"))
            self.assertEqual(events[-1].result.status, "completed")
            self.assertEqual(entered, ["seed.txt"])
            self.assertIn("read-1", str(client.calls[1]["input"]))
            self.assertIn("资料正文", str(client.calls[1]["input"]))
            self.assertIn("读取后先分析", str(client.calls[1]["input"]))
            self.assertFalse(control.interrupt_controller.interrupted)

    def test_empty_or_invalid_mode_never_interrupts(self):
        control = RunControl()
        with control.model_request() as request:
            for content, mode in ((" ", "interrupt"), ("输入", "unknown")):
                with self.assertRaises(ValueError):
                    control.steer(content, mode=mode)
            self.assertFalse(request.interrupted)
            self.assertFalse(control.steering_mailbox.has_pending)

    def test_consecutive_interrupts_use_fresh_request_signals_and_keep_fifo(self):
        with TemporaryDirectory() as directory:
            engine = AgentEngine.create(HexgentConfig(), Path(directory))
            control = RunControl()
            first, second = GatedStream(), GatedStream()
            client = Client([first, second, [completed()]])
            with patch("hexgent.loop.agent_turn.get_model", return_value="offline"), ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: list(engine.run("原始任务", client=client, run_control=control)))
                try:
                    self.assertTrue(first.waiting.wait(5))
                    control.steer("第一条更正", mode="interrupt")
                    self.assertTrue(second.waiting.wait(5))
                    control.steer("第二条更正", mode="interrupt")
                finally:
                    first.released.set()
                    second.released.set()
                events = future.result(timeout=5)
            self.assertEqual(events[-1].result.status, "completed")
            self.assertEqual(len(client.calls), 3)
            latest = str(client.calls[2]["input"])
            self.assertLess(latest.index("第一条更正"), latest.index("第二条更正"))
            records = engine.session.store.read_transcript()
            self.assertEqual(sum(r["type"] == "agent_run_started" for r in records), 1)
            self.assertEqual(sum(r["type"] == "model_request_yielded" for r in records), 2)
            final = next(r["payload"]["run_state"] for r in reversed(records) if "run_state" in r["payload"])
            self.assertEqual(final["model_rounds"], 3)

    def test_request_preemption_callback_can_escalate_to_cancel(self):
        control = RunControl()
        with control.model_request() as request:
            request.add_callback(control.cancel)
            control.steer("待纠偏", mode="interrupt")
            self.assertTrue(request.interrupted)
            self.assertTrue(control.interrupt_controller.interrupted)
            self.assertEqual(control.terminal_action, "cancel")

    def test_interrupted_attempt_consumes_model_round_budget(self):
        config = replace(HexgentConfig(), loop=LoopLimits(max_model_rounds=1))
        calls, records, result, _, _ = self.run_gated(config=config)
        self.assertEqual(result.stop_reason, "max_model_rounds")
        self.assertEqual(len(calls), 2)  # 第二次仅是原有预算收尾请求。
        self.assertFalse(calls[1].get("tools"))
        self.assertTrue(any(r["type"] == "model_request_yielded" for r in records))


if __name__ == "__main__":
    unittest.main()
