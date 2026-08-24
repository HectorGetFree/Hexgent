"""会话 Transcript 和大工具结果持久化测试。"""

import json
from dataclasses import FrozenInstanceError
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.context.context import ConversationContext
from hexgent.context.session_store import (
    SessionStore,
    ToolResultLimits,
    build_persisted_output_message,
)
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import ToolExecutionResult


tool_execution_module = import_module("hexgent.tool.tool_execution")


def drain_generator(generator):
    """收集生成器事件，并取得工具批次最终返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


class SessionStoreTest(unittest.TestCase):
    """验证会话事实和大结果可以从磁盘恢复。"""

    def test_user_query_is_appended_to_jsonl_verbatim(self) -> None:
        """用户 Query 应以原文写入追加式 Transcript。"""
        with TemporaryDirectory() as directory:
            conversation = ConversationContext.persistent(Path(directory))
            conversation.append_user_query("原始 Query：不要修改空格  和标点！")
            transcript_path = conversation.session_store.transcript_path
            records = [
                json.loads(line)
                for line in transcript_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(records[0]["type"], "session_started")
        self.assertEqual(records[1]["type"], "user_query")
        self.assertTrue(records[0]["created_at"].endswith("+08:00"))
        self.assertTrue(records[1]["created_at"].endswith("+08:00"))
        self.assertEqual(
            records[1]["payload"]["content"],
            "原始 Query：不要修改空格  和标点！",
        )
        self.assertRegex(
            conversation.session_store.session_dir.name,
            r"^\d{8}-\d{6}_原始 Query：不要修改空格 和标点！$",
        )
        with self.assertRaises(FrozenInstanceError):
            conversation.user_queries[0].content = "试图改写"

    def test_large_tool_result_is_persisted_with_preview(self) -> None:
        """大工具结果应完整落盘，模型上下文只保留路径和预览。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(
                root,
                session_id="session-test",
                tool_result_limits=ToolResultLimits(
                    max_inline_characters=10,
                    preview_characters=5,
                ),
            )
            output = "0123456789完整内容"
            persisted = store.persist_tool_result("call/1", "bash", output)
            message = build_persisted_output_message(persisted)
            result_path = root / persisted.relative_path

            self.assertEqual(result_path.read_text(encoding="utf-8"), output)
            self.assertIn("path=", message)
            self.assertIn("01234", message)
            self.assertNotIn(output, message)

    def test_tool_batch_returns_persisted_output_reference(self) -> None:
        """工具批次应把大结果替换为带路径的配对结果。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(
                root,
                session_id="session-batch",
                tool_result_limits=ToolResultLimits(
                    max_inline_characters=10,
                    preview_characters=5,
                ),
            )
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            tool_call = SimpleNamespace(
                call_id="call-1",
                name="bash",
                arguments='{"command":"example"}',
            )

            with patch.object(
                tool_execution_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "A" * 20, False),
            ):
                _, batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        [tool_call],
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=1,
                        deadline=monotonic() + 10,
                        session_store=store,
                    )
                )

            output = batch.outputs[0]
            self.assertEqual(output["call_id"], "call-1")
            self.assertIn("<persisted-output", output["output"])
            self.assertTrue((store.tool_results_dir / "call-1.txt").exists())
            records = [
                json.loads(line)
                for line in store.transcript_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[-2]["type"], "tool_result_persisted")
            self.assertEqual(records[-1]["type"], "tool_execution_finished")
            self.assertIsNone(records[-1]["payload"]["output"])
            self.assertEqual(
                records[-1]["payload"]["persisted_output"],
                ".hexgent/sessions/session-batch/tool-results/call-1.txt",
            )

    def test_runtime_events_are_written_in_order(self) -> None:
        """Transcript 应保留不进入模型上下文的完整运行时事件顺序。"""
        with TemporaryDirectory() as directory:
            conversation = ConversationContext.persistent(Path(directory))
            conversation.append_user_query("检查上下文模块")
            conversation.record_runtime_event(
                "agent_run_started",
                {"limits_enabled": False},
            )
            conversation.append_message(
                {"role": "assistant", "content": "正在检查。"}
            )
            conversation.record_runtime_event(
                "agent_run_finished",
                {"status": "completed"},
            )
            records = [
                json.loads(line)
                for line in conversation.session_store.transcript_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(
            [record["type"] for record in records],
            [
                "session_started",
                "user_query",
                "agent_run_started",
                "message",
                "agent_run_finished",
            ],
        )

if __name__ == "__main__":
    unittest.main()
