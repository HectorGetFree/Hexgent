"""纠偏持久化、提交间隙故障和正式恢复验收；只使用 FakeModel。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.loop.loop_state import LoopState
from hexgent.loop.agent_turn import _continue_with_steering
from hexgent.plan import InMemoryPlanStore
from hexgent.run_control import RunControl
from hexgent.session import Session
from hexgent.tool.tool_context import ToolExecutionContext
from tests.test_interrupting_steer import Client, completed


def crash_after_steer(root, session_id, after_context):
    """独立进程只从磁盘构建 Runtime，提交后直接退出，不运行 finally。"""
    engine = AgentEngine.restore(HexgentConfig(), Path(root), session_id)
    state = engine._build_run_state(RunRequest.continue_run(), HexgentConfig())
    engine.session.start_run(limits_enabled=True, plan=None, loop_state=state)
    control = RunControl()
    control.bind_steering_session(engine.session)
    control.steer("跨进程唯一输入", command_id="process-steer")
    if after_context:
        original = engine.session.record_runtime_control_effect
        def stop(identity, effect, payload=None):
            if effect == "steering_applied":
                os._exit(73)
            return original(identity, effect, payload)
        engine.session.record_runtime_control_effect = stop
        _continue_with_steering(state, control, engine.session, HexgentConfig())
    os._exit(73)


class DurableSteeringTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = ContextManager()
        self.session = Session.persistent(self.root)
        self.session.record_user_query(self.context.append_user_query("原始任务"))
        self.state = LoopState(self.context, ToolExecutionContext(InMemoryPlanStore(), self.root))
        self.session.start_run(limits_enabled=True, plan=None, loop_state=self.state)
        self.session.create_checkpoint(context_manager=self.context, plan=None,
            reason="tool_batch_completed", loop_state=self.state, run_status="active")
        self.control = RunControl()
        self.control.bind_steering_session(self.session)

    def resume(self):
        engine = AgentEngine.restore(HexgentConfig(), self.root, self.session.store.session_id)
        client = Client([[completed()]])
        with patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
            list(engine.run(RunRequest.continue_run(), client=client))
        return engine, client

    def test_pending_intent_survives_formal_restore_continue(self):
        self.control.steer("先核验风险", mode="interrupt", command_id="s1")
        engine, client = self.resume()
        self.assertIn("先核验风险", str(client.calls[0]["input"]))
        self.assertEqual(len(engine.session.steering_context_mutations("s1")), 1)
        self.assertTrue(engine.session.has_runtime_effect("s1", "steering_applied"))

    def test_context_commit_without_marker_is_not_injected_twice(self):
        self.control.steer("唯一更正", command_id="s2")
        original = self.session.record_runtime_control_effect
        def fail(identity, effect, payload=None):
            if effect == "steering_applied":
                raise OSError("注入提交后标记失败")
            return original(identity, effect, payload)
        with patch.object(self.session, "record_runtime_control_effect", side_effect=fail):
            with self.assertRaises(OSError):
                _continue_with_steering(self.state, self.control, self.session, HexgentConfig())
        engine, client = self.resume()
        self.assertEqual(len(engine.session.steering_context_mutations("s2")), 1)
        self.assertEqual(str(client.calls[0]["input"]).count("唯一更正"), 1)
        self.assertTrue(engine.session.has_runtime_effect("s2", "steering_applied"))

    def test_duplicate_and_conflicting_command_after_rebuild(self):
        self.control.steer("原文", command_id="s3")
        engine = AgentEngine.restore(HexgentConfig(), self.root, self.session.store.session_id)
        control = RunControl()
        control.bind_steering_session(engine.session)
        control.steer("原文", command_id="s3")
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            control.steer("不同原文", command_id="s3")
        self.assertEqual(len(engine.session.steering_intents()), 1)

    def test_intent_write_failure_does_not_queue_or_interrupt(self):
        with self.control.model_request() as request:
            with patch.object(self.session.store, "append_transcript", side_effect=OSError("写入失败")):
                with self.assertRaises(OSError):
                    self.control.steer("不能执行", mode="interrupt", command_id="s4")
            self.assertFalse(request.interrupted)
            self.assertFalse(self.control.steering_mailbox.has_pending)

    def test_context_write_failure_preserves_pending_durable_intent(self):
        self.control.steer("恢复后执行", command_id="s5")
        with patch.object(self.session, "record_user_query", side_effect=OSError("消息提交失败")):
            with self.assertRaises(OSError):
                _continue_with_steering(self.state, self.control, self.session, HexgentConfig())
        engine, client = self.resume()
        self.assertEqual(len(engine.session.steering_context_mutations("s5")), 1)
        self.assertIn("恢复后执行", str(client.calls[0]["input"]))

    def test_fifo_and_pause_leave_pending_intents(self):
        for i in range(3):
            self.control.steer(f"纠偏{i}", command_id=f"fifo-{i}")
        self.control.pause()
        self.assertEqual(self.control.drain_steering(), [])
        engine, client = self.resume()
        request = str(client.calls[0]["input"])
        self.assertLess(request.index("纠偏0"), request.index("纠偏1"))
        self.assertLess(request.index("纠偏1"), request.index("纠偏2"))

    def test_applied_command_is_not_reapplied_after_restore(self):
        self.control.steer("已应用", command_id="s6")
        _continue_with_steering(self.state, self.control, self.session, HexgentConfig())
        engine, _ = self.resume()
        self.assertEqual(len(engine.session.steering_context_mutations("s6")), 1)

    def test_prebound_queue_is_persisted_at_run_entry(self):
        control = RunControl()
        control.steer("预排队", command_id="s7")
        control.steer("预排队", command_id="s7")
        control.bind_steering_session(self.session)
        self.assertEqual(len(self.session.steering_intents()), 1)
        self.assertEqual(len(control.drain_steering()), 1)

    def test_real_process_exit_after_intent_or_context_commit(self):
        for after_context in (False, True):
            with self.subTest(after_context=after_context), TemporaryDirectory() as directory:
                # 每个窗口使用不同 Session，避免复用上一窗口的成功状态。
                root = Path(directory)
                engine = AgentEngine.create(HexgentConfig(), root)
                state = engine._build_run_state(RunRequest.submit("原始任务"), HexgentConfig())
                engine.session.start_run(limits_enabled=True, plan=None, loop_state=state)
                engine.session.create_checkpoint(context_manager=engine.context_manager, plan=None,
                    reason="tool_batch_completed", loop_state=state, run_status="active")
                identity = engine.session.store.session_id
                result = subprocess.run([sys.executable, "-c",
                    "from tests.test_durable_steering import crash_after_steer; import sys; "
                    "crash_after_steer(sys.argv[1],sys.argv[2],sys.argv[3]=='True')",
                    str(root), identity, str(after_context)], capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 73, result.stderr.decode())
                restored = AgentEngine.restore(HexgentConfig(), root, identity)
                client = Client([[completed()]])
                with patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
                    list(restored.run(RunRequest.continue_run(), client=client))
                self.assertEqual(str(client.calls[0]["input"]).count("跨进程唯一输入"), 1)
                self.assertEqual(len(restored.session.steering_context_mutations("process-steer")), 1)

    def test_concurrent_same_command_commits_one_intent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.control.steer("并发重复", command_id="same"), range(8)))
        self.assertEqual(len(self.session.steering_intents()), 1)
        self.assertEqual(len(self.control.drain_steering()), 1)

    def test_unknown_inflight_model_budget_still_blocks_continue(self):
        self.session.invalidate_run_state("main_model")
        self.control.steer("尚未应用但不能丢失", command_id="uncertain")
        engine = AgentEngine.restore(HexgentConfig(), self.root, self.session.store.session_id)
        self.assertFalse(engine.has_continuable_run)
        self.assertEqual(len(engine.session.pending_steering_intents()), 1)
        client = Client([[completed()]])
        with self.assertRaisesRegex(RuntimeError, "没有可继续"):
            list(engine.run(RunRequest.continue_run(), client=client))
        self.assertEqual(client.calls, [])
