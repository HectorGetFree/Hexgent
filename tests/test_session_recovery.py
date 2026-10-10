"""验证 Transcript 可以恢复一个可继续运行的 Hexgent Core Session。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.loop.loop_state import LoopState
from hexgent.plan import Plan, PlanStep
from hexgent.session import (
    Session,
    SessionRestorer,
    SessionSettings,
    build_persisted_output_message,
)
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.plan import InMemoryPlanStore


class SessionRecoveryTest(unittest.TestCase):
    """覆盖 Context、Plan、压缩和中断工具协议的恢复边界。"""

    def test_restores_context_plan_settings_and_continues_sequence(self) -> None:
        """恢复后应继承核心状态，并从原 Transcript 序号继续追加。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(
                root,
                settings=SessionSettings("accept_edits"),
            )
            query = context.append_user_query("检查恢复能力")
            session.record_user_query(query)
            answer = {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "已经完成检查"}],
            }
            context.append_messages([answer], completes_round=True)
            session.record_context_messages(
                [answer],
                completes_round=True,
                source="model_response",
            )
            plan = Plan(
                "完成恢复",
                "in_progress",
                [PlanStep("继续测试", "正在继续测试", "in_progress", "step-1")],
            )
            session.record_plan_state(plan, action="created")
            session_id = session.store.session_id
            next_sequence = session.store.next_sequence

            restored = SessionRestorer(root).restore(session_id)
            restored_query = restored.context_manager.append_user_query("继续执行")
            restored.session.record_user_query(restored_query)
            records = restored.session.store.read_transcript()

        self.assertEqual(restored.session.settings.permission_mode, "accept_edits")
        self.assertEqual(restored.context_manager.round_boundaries, [2])
        self.assertEqual(restored.context_manager.user_queries[0].content, "检查恢复能力")
        self.assertEqual(
            restored.tool_context.plan_store.get_plan(),
            plan,
        )
        self.assertEqual(records[-1]["sequence"], next_sequence + 1)
        self.assertEqual(records[-1]["payload"]["content"], "继续执行")

    def test_checkpoint_serializes_sdk_style_response_items(self) -> None:
        """Checkpoint 应将 SDK 响应对象转成恢复可用的普通消息字典。"""
        class ResponseOutputMessage:
            def model_dump(self, *, mode: str | None = None) -> dict[str, object]:
                self_mode = mode
                return {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "已完成"}],
                    "serialization_mode": self_mode,
                }

        class ResponseReasoningItem:
            def model_dump(self, *, mode: str | None = None) -> dict[str, object]:
                return {
                    "type": "reasoning",
                    "content": [],
                    "serialization_mode": mode,
                }

        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            query = context.append_user_query("验证 SDK 响应快照")
            session.record_user_query(query)
            context.append_messages(
                [ResponseReasoningItem(), ResponseOutputMessage()],
                completes_round=True,
            )
            checkpoint = session.create_checkpoint(
                context_manager=context,
                plan=None,
                reason="tool_batch_completed",
                loop_state=None,
                run_status="terminal",
            )
            restored = SessionRestorer(root).restore(session.store.session_id)

        self.assertEqual(checkpoint.context["active_messages"][1]["type"], "reasoning")
        self.assertEqual(checkpoint.context["active_messages"][2]["type"], "message")
        self.assertEqual(restored.context_manager.model_messages()[2]["content"][0]["text"], "已完成")

    def test_uses_earlier_committed_checkpoint_when_latest_is_corrupted(self) -> None:
        """最新快照损坏时应回退更早提交快照并重放增量事实。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            first = context.append_user_query("第一条请求")
            session.record_user_query(first)
            first_checkpoint = session.create_checkpoint(
                context_manager=context,
                plan=None,
                reason="tool_batch_completed",
                loop_state=None,
                run_status="terminal",
            )
            second = context.append_user_query("第二条请求")
            session.record_user_query(second)
            latest_checkpoint = session.create_checkpoint(
                context_manager=context,
                plan=None,
                reason="tool_batch_completed",
                loop_state=None,
                run_status="terminal",
            )
            checkpoint_path = (
                session.store.session_dir
                / "checkpoints"
                / f"{latest_checkpoint.checkpoint_id}.json"
            )
            checkpoint_path.write_text("损坏的 Checkpoint", encoding="utf-8")

            restored = SessionRestorer(root).restore(session.store.session_id)

        self.assertEqual(first_checkpoint.checkpoint_id, "cp-000001")
        self.assertEqual(
            [query.content for query in restored.context_manager.user_queries],
            ["第一条请求", "第二条请求"],
        )

    def test_restores_pending_run_state_only_from_safe_checkpoint(self) -> None:
        """可续跑 Checkpoint 不应被新的状态事实静默覆盖。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            query = context.append_user_query("继续未完成任务")
            session.record_user_query(query)
            state = LoopState(
                context,
                ToolExecutionContext(InMemoryPlanStore(), root),
                model_rounds=2,
                tool_calls=4,
            )
            session.create_checkpoint(
                context_manager=context,
                plan=None,
                reason="tool_batch_completed",
                loop_state=state,
                run_status="active",
            )

            restored = SessionRestorer(root).restore(session.store.session_id)

        self.assertIsNotNone(restored.pending_run_state)
        self.assertEqual(restored.pending_run_state.model_rounds, 2)
        self.assertEqual(restored.pending_run_state.tool_calls, 4)

    def test_restores_committed_compaction_exactly(self) -> None:
        """恢复端应采用压缩后的消息和边界，不再次调用摘要模型。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            first = context.append_user_query("较早问题")
            session.record_user_query(first)
            answer = {"role": "assistant", "content": "较早回答"}
            context.append_messages([answer], completes_round=True)
            session.record_context_messages(
                [answer],
                completes_round=True,
                source="model_response",
            )
            latest = context.append_user_query("最新问题")
            session.record_user_query(latest)
            boundary = context.apply_compaction(
                2,
                "此前已经完成较早问题。",
                before_tokens=100,
                after_tokens=30,
                summary_tokens=10,
                recent_tokens=5,
                status="compacted",
            )
            session.record_compaction(boundary)
            expected_messages = context.model_messages()
            expected_boundaries = list(context.round_boundaries)

            restored = SessionRestorer(root).restore(session.store.session_id)

        self.assertEqual(restored.context_manager.model_messages(), expected_messages)
        self.assertEqual(restored.context_manager.round_boundaries, expected_boundaries)
        self.assertEqual(restored.context_manager.compaction_count, 1)

    def test_repairs_unstarted_tool_call_without_retrying_it(self) -> None:
        """尚未进入工具管线的 Call 应明确标为未执行且只修复一次。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            query = context.append_user_query("修改文件")
            session.record_user_query(query)
            tool_call = {
                "type": "function_call",
                "name": "write_file",
                "call_id": "call-interrupted",
                "arguments": '{"path":"example.txt","content":"内容"}',
            }
            context.append_messages([tool_call], completes_round=False)
            session.record_context_messages(
                [tool_call],
                completes_round=False,
                source="model_response",
            )
            session_id = session.store.session_id

            first_restore = SessionRestorer(root).restore(session_id)
            second_restore = SessionRestorer(root).restore(session_id)

        outputs = [
            message
            for message in second_restore.context_manager.active_messages
            if isinstance(message, dict)
            and message.get("type") == "function_call_output"
        ]
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["call_id"], "call-interrupted")
        self.assertIn("未执行", outputs[0]["output"])
        self.assertEqual(first_restore.context_manager.round_boundaries, [3])

    def test_recovery_uses_dispatch_ledger_to_distinguish_retry_safety(self) -> None:
        """已派发的只读与副作用工具必须得到不同的保守恢复结论。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            query = context.append_user_query("恢复中断工具")
            session.record_user_query(query)
            calls = [
                {
                    "type": "function_call",
                    "name": "read_file",
                    "call_id": "call-read",
                    "arguments": '{"path":"README.md"}',
                },
                {
                    "type": "function_call",
                    "name": "write_file",
                    "call_id": "call-write",
                    "arguments": '{"path":"README.md","content":"x"}',
                },
            ]
            context.append_messages(calls, completes_round=False)
            session.record_context_messages(
                calls,
                completes_round=False,
                source="model_response",
            )
            assert session.store is not None
            for call_id, tool_name, action in (
                ("call-read", "read_file", "safe_to_retry"),
                ("call-write", "write_file", "verify_before_retry"),
            ):
                session.store.append_transcript(
                    "tool_execution_started",
                    {"call_id": call_id, "tool_name": tool_name},
                )
                session.store.append_transcript(
                    "tool_execution_dispatched",
                    {
                        "call_id": call_id,
                        "tool_name": tool_name,
                        "recovery_action": action,
                    },
                )
            restored = SessionRestorer(root).restore(session.store.session_id)

        outputs = {
            message["call_id"]: message["output"]
            for message in restored.context_manager.active_messages
            if isinstance(message, dict)
            and message.get("type") == "function_call_output"
        }
        self.assertIn("可以按需重新调用", outputs["call-read"])
        self.assertIn("副作用状态未知", outputs["call-write"])

    def test_restores_finished_tool_output_missing_from_context_exactly_once(self) -> None:
        """finished 已落盘但 Context 输出遗漏时，应重放同一模型可见结果而不重跑工具。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            session.record_user_query(context.append_user_query("恢复外置工具结果"))
            call = {
                "type": "function_call",
                "name": "read_file",
                "call_id": "call-finished",
                "arguments": '{"path":"README.md"}',
            }
            context.append_messages([call], completes_round=False)
            session.record_context_messages(
                [call], completes_round=False, source="model_response"
            )
            assert session.store is not None
            persisted = session.store.persist_tool_result(
                "call-finished",
                "read_file",
                "完整结果" * 4_000,
                force=True,
                description="read_file(README.md)",
            )
            assert persisted is not None
            session.store.append_transcript(
                "tool_execution_started",
                {"call_id": "call-finished", "tool_name": "read_file"},
            )
            session.store.append_transcript(
                "tool_execution_dispatched",
                {
                    "call_id": "call-finished",
                    "tool_name": "read_file",
                    "recovery_action": "safe_to_retry",
                },
            )
            session.store.append_transcript(
                "tool_execution_finished",
                {
                    "call_id": "call-finished",
                    "tool_name": "read_file",
                    "status": "success",
                    "output": None,
                    "persisted_output": persisted.relative_path,
                },
            )

            first = SessionRestorer(root).restore(session.store.session_id)
            second = SessionRestorer(root).restore(session.store.session_id)

        first_outputs = [
            item
            for item in first.context_manager.active_messages
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
        ]
        second_outputs = [
            item
            for item in second.context_manager.active_messages
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
        ]
        self.assertEqual(len(first_outputs), 1)
        self.assertEqual(len(second_outputs), 1)
        self.assertEqual(
            first_outputs[0]["output"], build_persisted_output_message(persisted)
        )

    def test_cleared_plan_is_not_restored(self) -> None:
        """最后一次 Plan 状态为空时不得恢复此前已经完成的计划。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            session.record_user_query(context.append_user_query("完成任务"))
            plan = Plan(
                "完成任务",
                "completed",
                [PlanStep("完成", "正在完成", "completed", "step-1")],
            )
            session.record_plan_state(plan, action="updated")
            session.record_plan_state(None, action="cleared", reason="completed")

            restored = AgentEngine.restore(
                HexgentConfig(),
                root,
                session.store.session_id,
            )

        self.assertIsNone(restored.tool_context.plan_store.get_plan())
        self.assertEqual(restored.context_manager.user_queries[0].content, "完成任务")


if __name__ == "__main__":
    unittest.main()
