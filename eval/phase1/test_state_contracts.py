"""第一阶段：Agent Runtime 状态与事件契约。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.config import HexgentConfig
from hexgent.events import LoopLimitReachedEvent, LoopTransitionEvent, TextDeltaEvent
from hexgent.loop.limits import LoopLimits
from hexgent.loop.loop_state import LoopState
from hexgent.context import ContextManager
from hexgent.plan import InMemoryPlanStore, Plan, PlanStep
from hexgent.run_control import RunControl
from hexgent.session import Session
from hexgent.session.session import SessionSettings
from hexgent.session.checkpoint import CheckpointStore
from hexgent.tool.plan_tools import validate_update_plan
from hexgent.tool.tool_result import ToolExecutionResult
from hexgent.tool.tool_context import ToolExecutionContext

from eval.support.agent_contracts import run_trace, text_response, tool_call


class AgentStateContractTest(unittest.TestCase):
    """验证最小状态机、控制流和持久化契约。"""

    def test_text_only_run_has_one_terminal_event(self) -> None:
        trace, _ = run_trace([text_response("完成")])
        self.assertEqual([event.delta for event in trace.events if isinstance(event, TextDeltaEvent)], ["完成"])
        self.assertEqual(trace.finished.result.status, "completed")
        trace.assert_terminal_last()

    def test_tool_transition_has_checkpoint_and_terminal(self) -> None:
        with TemporaryDirectory() as directory:
            session = Session.persistent(Path(directory), settings=SessionSettings())
            session.record_user_query(SimpleNamespace(sequence=1, content="读取 a"))
            trace, _ = run_trace(
                [text_response(output=[tool_call("call-1")]), text_response("完成")],
                session=session,
                workdir=Path(directory),
            )
            trace.assert_tool_pairs()
            self.assertTrue(any(isinstance(event, LoopTransitionEvent) for event in trace.events))
            self.assertEqual(trace.finished.result.status, "completed")
            self.assertTrue(any(record["type"] == "checkpoint_committed" for record in trace.transcript))

    def test_pause_is_continuable_and_cancel_is_terminal(self) -> None:
        control = RunControl()
        control.pause()
        trace, _ = run_trace([text_response("不会继续请求")], run_control=control)
        self.assertEqual(trace.finished.result.status, "interrupted")

        control = RunControl()
        control.cancel()
        trace, _ = run_trace([text_response("不会继续请求")], run_control=control)
        self.assertEqual(trace.finished.result.status, "cancelled")

    def test_steering_is_a_transition_before_next_model_request(self) -> None:
        control = RunControl()
        control.steer("补充检查配置文件")
        trace, model = run_trace([text_response("已完成补充检查")], run_control=control)
        transitions = [event.reason for event in trace.events if isinstance(event, LoopTransitionEvent)]
        self.assertIn("steering_applied", transitions)
        self.assertEqual(trace.finished.result.status, "completed")
        self.assertEqual(model.calls[0]["input"][0]["content"], "补充检查配置文件")

    def test_plan_blocked_and_incomplete_are_distinct_terminals(self) -> None:
        blocked_context = ToolExecutionContext(InMemoryPlanStore(), Path.cwd())
        blocked_context.plan_store.set_plan(
            Plan("目标", "blocked", [PlanStep("等待输入", "等待输入", "pending", "step-1")], blocked_reason="缺少凭证")
        )
        trace, _ = run_trace([text_response("需要补充凭证")], tool_context=blocked_context)
        self.assertEqual(trace.finished.result.status, "blocked")
        self.assertEqual(trace.finished.result.stop_reason, "plan_blocked")

        incomplete_context = ToolExecutionContext(InMemoryPlanStore(), Path.cwd())
        incomplete_context.plan_store.set_plan(
            Plan("目标", "in_progress", [PlanStep("执行工作", "执行工作", "in_progress", "step-1")])
        )
        trace, _ = run_trace(
            [text_response("暂时无法继续")],
            tool_context=incomplete_context,
            config=HexgentConfig(loop=LoopLimits(max_plan_completion_nudges=0)),
        )
        self.assertEqual(trace.finished.result.status, "incomplete")
        self.assertEqual(trace.finished.result.stop_reason, "plan_still_active")

    def test_budget_limit_enters_finalization_before_terminal(self) -> None:
        trace, _ = run_trace(
            [text_response("预算收尾")],
            config=HexgentConfig(loop=LoopLimits(max_model_rounds=0)),
        )
        self.assertTrue(any(isinstance(event, LoopLimitReachedEvent) for event in trace.events))
        self.assertTrue(any(
            isinstance(event, LoopTransitionEvent)
            and event.reason == "budget_finalization"
            for event in trace.events
        ))
        self.assertEqual(trace.finished.result.status, "limit_reached")

    def test_loop_state_snapshot_round_trip_preserves_runtime_fields(self) -> None:
        state = LoopState(
            context_manager=ContextManager(),
            tool_context=ToolExecutionContext(InMemoryPlanStore(), Path.cwd()),
            model_rounds=3,
            tool_calls=4,
            input_tokens=50,
            output_tokens=20,
            transition="tool_results_ready",
        )
        restored = LoopState.from_snapshot(
            state.to_snapshot(),
            context_manager=state.context_manager,
            tool_context=state.tool_context,
        )
        self.assertEqual(restored.model_rounds, 3)
        self.assertEqual(restored.tool_calls, 4)
        self.assertEqual(restored.input_tokens, 50)
        self.assertEqual(restored.output_tokens, 20)
        self.assertEqual(restored.transition, "tool_results_ready")

    def test_model_failure_is_a_failed_terminal(self) -> None:
        trace, _ = run_trace([RuntimeError("模型连接失败")])
        self.assertEqual(trace.finished.result.status, "failed")
        self.assertIn("模型流式响应错误", trace.finished.result.output_text)
        trace.assert_terminal_last()

    def test_permission_denial_returns_tool_error_then_can_finish(self) -> None:
        trace, _ = run_trace(
            [text_response(output=[tool_call("denied-1")]), text_response("已处理")],
        )
        # 默认 read_file 是 allow；用不存在文件验证工具错误仍回填并继续，而不是 Runtime 终止。
        finished = [event for event in trace.events if event.__class__.__name__ == "ToolFinishedEvent"]
        self.assertEqual(finished[0].status, "error")
        self.assertEqual(trace.finished.result.status, "completed")

    def test_checkpoint_checksum_corruption_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory)
            store = Session.persistent(path)
            store.record_user_query(SimpleNamespace(sequence=1, content="创建快照"))
            checkpoint = store.create_checkpoint(
                context_manager=ContextManager(),
                plan=None,
                reason="run_finished",
                loop_state=None,
                run_status="terminal",
            )
            assert checkpoint is not None
            checkpoint_path = path / ".hexgent" / "sessions" / store.store.session_id / "checkpoints" / f"{checkpoint.checkpoint_id}.json"
            checkpoint_path.write_text(checkpoint_path.read_text(encoding="utf-8").replace('"checksum":"', '"checksum":"broken', 1), encoding="utf-8")
            with self.assertRaises(ValueError):
                CheckpointStore(checkpoint_path.parent.parent).read(checkpoint.checkpoint_id)

    def test_plan_update_rejects_unknown_step_identity_and_revision(self) -> None:
        context = ToolExecutionContext(InMemoryPlanStore(), Path.cwd())
        context.plan_store.set_plan(
            Plan("目标", "in_progress", [PlanStep("读取", "读取", "in_progress", "step-1")])
        )
        unknown_step = validate_update_plan(
            {
                "expected_revision": 1,
                "status": "in_progress",
                "reason": None,
                "blocked_reason": None,
                "steps": [{"step_id": "step-x", "content": "读取", "process_message": "读取", "status": "in_progress"}],
            },
            context,
        )
        self.assertIn("未知 step_id", unknown_step or "")
        stale_revision = validate_update_plan(
            {
                "expected_revision": 0,
                "status": "in_progress",
                "reason": None,
                "blocked_reason": None,
                "steps": [{"step_id": "step-1", "content": "读取", "process_message": "读取", "status": "in_progress"}],
            },
            context,
        )
        self.assertIn("版本冲突", stale_revision or "")


if __name__ == "__main__":
    unittest.main()
