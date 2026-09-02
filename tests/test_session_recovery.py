"""验证 Transcript 可以恢复一个可继续运行的 Hexgent Core Session。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.plan import Plan, PlanStep
from hexgent.session import Session, SessionRestorer, SessionSettings


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

    def test_repairs_unclosed_tool_call_without_retrying_it(self) -> None:
        """崩溃留下的 Tool Call 应获得未知副作用结果且只修复一次。"""
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
        self.assertIn("副作用未知", outputs[0]["output"])
        self.assertEqual(first_restore.context_manager.round_boundaries, [3])

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
