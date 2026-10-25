"""工具阶段纠偏：真实 Pipeline、配对持久化及 Event 同步，不调用模型服务。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import json
from time import monotonic
import unittest
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.run_control import RunControl
from hexgent.session.session_store import SessionStore
from hexgent.tool.tool_registry import TOOL_REGISTRY
from hexgent.tool.tool_result import ToolHandlerResult
from hexgent.tool.tool_retry import ToolRetryLimits
from hexgent.tool.tool_scheduler import ToolParallelLimits
from tests.test_interrupting_steer import Client, completed


def call(identity, name="read_file", **arguments):
    return SimpleNamespace(type="function_call", call_id=identity, name=name,
                           arguments=json.dumps(arguments))


class ToolSteeringTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("a.txt", "b.txt", "c.txt"):
            (self.root / name).write_text(name, encoding="utf-8")
        self.control = RunControl()

    def run_engine(self, calls, *, config=None, permission=None):
        self.engine = AgentEngine.create(config or HexgentConfig(), self.root)
        self.client = Client([[completed(output=calls)], [completed()]])
        with patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
            self.events = list(self.engine.run("读取或更新资料", client=self.client,
                run_control=self.control, user_confirmation=permission or (lambda *_: "allow")))
        self.records = self.engine.session.store.read_transcript()
        return self.events[-1].result

    def finishes(self):
        return [r["payload"] for r in self.records if r["type"] == "tool_execution_finished"]

    def dispatches(self):
        return [r["payload"] for r in self.records if r["type"] == "tool_execution_dispatched"]

    def test_steer_during_permission_blocks_handler_after_allow(self):
        def confirm(*_):
            self.control.steer("先不要写", mode="interrupt")
            return "allow"
        result = self.run_engine([call("write", "write_file", path="new.txt", content="old")], permission=confirm)
        self.assertEqual(result.status, "completed")
        self.assertFalse((self.root / "new.txt").exists())
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.finishes()[0]["attempt_count"], 0)
        self.assertEqual(self.finishes()[0]["failure"]["side_effect_state"], "not_started")
        self.assertIn("先不要写", str(self.client.calls[1]["input"]))
        final = next(r["payload"]["run_state"] for r in reversed(self.records) if "run_state" in r["payload"])
        self.assertEqual(final["tool_calls"], 0)

    def test_serial_write_preserves_commit_and_stops_later_groups(self):
        original = TOOL_REGISTRY["write_file"]
        entered = []
        def write(path, content, context):
            entered.append(path)
            result = original.function(path, content, context)
            self.control.steer("停止后续写入", mode="interrupt")
            return result
        with patch.dict(TOOL_REGISTRY, {"write_file": replace(original, function=write)}):
            result = self.run_engine([call("w1", "write_file", path="first.txt", content="committed"),
                call("w2", "write_file", path="second.txt", content="no"), call("r3", path="a.txt")])
        self.assertEqual(result.status, "completed")
        self.assertEqual(entered, ["first.txt"])
        self.assertEqual((self.root / "first.txt").read_text(), "committed")
        self.assertFalse((self.root / "second.txt").exists())
        self.assertEqual([r["call_id"] for r in self.finishes()], ["w1", "w2", "r3"])
        self.assertEqual([r["call_id"] for r in self.dispatches()], ["w1"])
        self.assertIn("w1", str(self.client.calls[1]["input"]))

    def test_parallel_queue_stops_and_inflight_read_settles_before_model_continues(self):
        original = TOOL_REGISTRY["read_file"]
        entered = []
        started, release, settled = Event(), Event(), Event()
        def read(path, context):
            entered.append(path)
            started.set()
            if not release.wait(5):
                raise AssertionError("测试未释放读取")
            result = original.function(path, context)
            settled.set()
            return result
        config = replace(HexgentConfig(), tool_parallel=ToolParallelLimits(max_workers=1))
        with patch.dict(TOOL_REGISTRY, {"read_file": replace(original, function=read)}), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.run_engine, [call("r1", path="a.txt"), call("r2", path="b.txt"), call("r3", path="c.txt")], config=config)
            try:
                self.assertTrue(started.wait(5))
                self.control.steer("先分析", mode="interrupt")
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(timeout=5)
        self.assertEqual(result.status, "completed")
        self.assertTrue(settled.is_set())
        self.assertEqual(entered, ["a.txt"])
        self.assertEqual([r["call_id"] for r in self.dispatches()], ["r1"])
        self.assertEqual([r["call_id"] for r in self.finishes()], ["r1", "r2", "r3"])
        self.assertEqual(self.finishes()[0]["status"], "success")
        self.assertTrue(all(r["attempt_count"] == 0 for r in self.finishes()[1:]))
        self.assertEqual(sum(r["type"] == "agent_run_started" for r in self.records), 1)
        self.assertEqual(sum(r["type"] == "steering_applied" for r in self.records), 1)

    def test_retry_and_duplicate_fallback_cannot_bypass_gate(self):
        for retry in (False, True):
            with self.subTest(retry=retry):
                self.control = RunControl()
                entered = []
                original = TOOL_REGISTRY["read_file"]
                def fail(path, context):
                    entered.append(path)
                    self.control.steer("不要重试", mode="interrupt")
                    return ToolHandlerResult.failed("temporary", "可重试失败", retryable=True, side_effect_state="not_started")
                config = replace(HexgentConfig(), tool_retry=ToolRetryLimits(enabled=retry, initial_backoff_seconds=0.001),
                                 tool_parallel=ToolParallelLimits(max_workers=1))
                with patch.dict(TOOL_REGISTRY, {"read_file": replace(original, function=fail, deduplication_policy="exact")}):
                    result = self.run_engine([call("r1", path="a.txt"), call("r2", path="a.txt")], config=config)
                self.assertEqual(result.status, "completed")
                self.assertEqual(entered, ["a.txt"])
                self.assertEqual(len(self.dispatches()), 1)
                self.assertEqual(self.finishes()[0]["attempt_count"], 1)
                self.assertEqual(self.finishes()[1]["attempt_count"], 0)

    def test_unknown_side_effect_is_kept_and_not_retried(self):
        entered = []
        original = TOOL_REGISTRY["write_file"]
        def unknown(path, content, context):
            entered.append(path)
            (context.workdir / path).write_text(content)
            self.control.steer("先核实写入", mode="interrupt")
            return ToolHandlerResult.failed("response_lost", "需要核验", retryable=True, side_effect_state="unknown")
        config = replace(HexgentConfig(), tool_retry=ToolRetryLimits(enabled=True, initial_backoff_seconds=0.001))
        with patch.dict(TOOL_REGISTRY, {"write_file": replace(original, function=unknown, automatic_retry_safe=True)}):
            self.run_engine([call("w", "write_file", path="unknown.txt", content="one")], config=config)
        self.assertEqual(entered, ["unknown.txt"])
        self.assertEqual(self.finishes()[0]["failure"]["side_effect_state"], "unknown")
        self.assertIn("需要核验", str(self.client.calls[1]["input"]))

    def test_boundary_mode_keeps_all_existing_tool_calls(self):
        original = TOOL_REGISTRY["read_file"]
        entered = []
        def read(path, context):
            entered.append(path)
            self.control.steer("边界要求")
            return original.function(path, context)
        config = replace(HexgentConfig(), tool_parallel=ToolParallelLimits(enabled=False))
        with patch.dict(TOOL_REGISTRY, {"read_file": replace(original, function=read)}):
            self.run_engine([call("r1", path="a.txt"), call("r2", path="b.txt")], config=config)
        self.assertEqual(entered, ["a.txt", "b.txt"])
        self.assertEqual(len(self.dispatches()), 2)

    def test_closed_or_preempted_gate_cannot_admit_late_work(self):
        admitted = []
        with self.control.tool_batch() as gate:
            self.assertTrue(gate.claim(lambda: admitted.append("before")))
            self.control.steer("停止", mode="interrupt")
            self.assertFalse(gate.claim(lambda: admitted.append("after")))
        self.assertFalse(gate.claim(lambda: admitted.append("late")))
        self.assertEqual(admitted, ["before"])

    def test_dispatch_persistence_failure_stops_before_handler(self):
        original = SessionStore.append_transcript
        def append(store, kind, payload):
            if kind == "tool_execution_dispatched":
                raise OSError("注入派发事实写入失败")
            return original(store, kind, payload)
        with patch.object(SessionStore, "append_transcript", new=append):
            with self.assertRaisesRegex(OSError, "派发事实写入失败"):
                self.run_engine([call("w", "write_file", path="never.txt", content="no")])
        self.assertFalse((self.root / "never.txt").exists())
        self.assertEqual(len(self.client.calls), 1)

    def test_batch_timeout_settles_started_handler_and_does_not_start_queued_one(self):
        original_read = TOOL_REGISTRY["read_file"]
        original_append = SessionStore.append_transcript
        started, release, settlement_wait = Event(), Event(), Event()
        entered = []
        def read(path, context):
            entered.append(path)
            started.set()
            if not release.wait(5):
                raise AssertionError("测试未释放超时读取")
            return original_read.function(path, context)
        def append(store, kind, payload):
            result = original_append(store, kind, payload)
            if kind == "tool_batch_settlement_wait":
                settlement_wait.set()
            return result
        def clock():
            return monotonic() + (100 if started.is_set() else 0)
        config = replace(HexgentConfig(), tool_parallel=ToolParallelLimits(max_workers=1, batch_timeout_seconds=1))
        with patch.dict(TOOL_REGISTRY, {"read_file": replace(original_read, function=read)}), \
                patch.object(SessionStore, "append_transcript", new=append), \
                patch("hexgent.tool.tool_scheduler.monotonic", side_effect=clock), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.run_engine, [call("r1", path="a.txt"), call("r2", path="b.txt")], config=config)
            try:
                self.assertTrue(settlement_wait.wait(5))
                self.assertFalse(future.done())
            finally:
                release.set()
            result = future.result(timeout=5)
        self.assertEqual(result.status, "completed")
        self.assertEqual(entered, ["a.txt"])
        self.assertEqual(self.finishes()[0]["status"], "success")
        self.assertEqual(self.finishes()[1]["attempt_count"], 0)
        self.assertEqual(self.finishes()[1]["failure"]["code"], "tool_timeout")
        self.assertEqual(len(self.dispatches()), 1)

    def test_denied_permission_is_not_overridden_by_gate(self):
        self.run_engine([call("w", "write_file", path="denied.txt", content="no")], permission=lambda *_: "deny")
        self.assertFalse((self.root / "denied.txt").exists())
        self.assertEqual(self.dispatches(), [])
        self.assertEqual(self.finishes()[0]["status"], "denied")


if __name__ == "__main__":
    unittest.main()
