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

from hexgent.session import (
    SessionStore,
    ToolResultLimits,
    build_persisted_output_message,
)
from hexgent.context import ContextManager
from hexgent.session import Session
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_context import ReadFileCacheEntry
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

    def test_session_does_not_own_context_manager(self) -> None:
        """Session 与 ContextManager 必须保持平级，不能形成领域包含关系。"""
        session = Session()
        context_manager = ContextManager()

        query = context_manager.append_user_query("检查能力边界")
        session.record_user_query(query)

        self.assertFalse(hasattr(session, "context_manager"))
        self.assertFalse(hasattr(session, "conversation"))
        self.assertEqual(context_manager.user_queries, (query,))

    def test_user_query_is_appended_to_jsonl_verbatim(self) -> None:
        """用户 Query 应以原文写入追加式 Transcript。"""
        with TemporaryDirectory() as directory:
            context_manager = ContextManager()
            session = Session.persistent(Path(directory))
            query = context_manager.append_user_query(
                "原始 Query：不要修改空格  和标点！"
            )
            session.record_user_query(query)
            transcript_path = session.store.transcript_path
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
            session.store.session_dir.name,
            r"^\d{8}-\d{6}_原始 Query：不要修改空格 和标点！$",
        )
        with self.assertRaises(FrozenInstanceError):
            context_manager.user_queries[0].content = "试图改写"

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
            persisted = store.persist_tool_result(
                "call/1",
                "read_file",
                output,
                description="read_file(tests/test_agent_loop_limits.py)",
            )
            message = build_persisted_output_message(persisted)
            result_path = root / persisted.relative_path

            self.assertEqual(result_path.read_text(encoding="utf-8"), output)
            self.assertEqual(
                result_path.name,
                "read_file_tests_test_agent_loop_limits.py__call_1.txt",
            )
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
            context.read_file_cache[root / "example.txt"] = ReadFileCacheEntry(
                "cached-hash"
            )
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
            result_paths = list(store.tool_results_dir.glob("*.txt"))
            self.assertEqual(len(result_paths), 1)
            self.assertEqual(
                result_paths[0].name,
                "bash_example__call-1.txt",
            )
            records = [
                json.loads(line)
                for line in store.transcript_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(records[-2]["type"], "tool_result_persisted")
            self.assertEqual(records[-1]["type"], "tool_execution_finished")
            self.assertIsNone(records[-1]["payload"]["output"])
            self.assertEqual(
                records[-1]["payload"]["persisted_output"],
                ".hexgent/sessions/session-batch/tool-results/bash_example__call-1.txt",
            )
            self.assertEqual(context.read_file_cache, {})

    def test_runtime_events_are_written_in_order(self) -> None:
        """Transcript 应保留不进入模型上下文的完整运行时事件顺序。"""
        with TemporaryDirectory() as directory:
            context_manager = ContextManager()
            session = Session.persistent(Path(directory))
            query = context_manager.append_user_query("检查上下文模块")
            session.record_user_query(query)
            session.record_runtime_event(
                "agent_run_started",
                {"limits_enabled": False},
            )
            message = {"role": "assistant", "content": "正在检查。"}
            context_manager.append_message(message)
            session.record_message(message)
            session.record_runtime_event(
                "agent_run_finished",
                {"status": "completed"},
            )
            records = [
                json.loads(line)
                for line in session.store.transcript_path.read_text(
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

    def test_batch_budget_persists_largest_inline_results(self) -> None:
        """多个小结果合计超限时应从最大结果开始外置。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(
                root,
                session_id="session-batch-budget",
                tool_result_limits=ToolResultLimits(
                    max_inline_characters=1_000,
                    preview_characters=20,
                    max_batch_inline_tokens=15,
                    max_batch_preview_tokens=10,
                ),
            )
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            context.read_file_cache[root / "example.txt"] = ReadFileCacheEntry(
                "cached-hash"
            )
            tool_calls = [
                SimpleNamespace(
                    call_id=f"call-{index}",
                    name="read_file",
                    arguments=f'{{"path":"{index}.txt"}}',
                )
                for index in range(1, 4)
            ]
            results = [
                ToolExecutionResult("success", "A" * 80, False),
                ToolExecutionResult("success", "B" * 60, False),
                ToolExecutionResult("success", "C" * 20, False),
            ]

            with patch.object(
                tool_execution_module,
                "execute_tool_call",
                side_effect=results,
            ):
                _, batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        tool_calls,
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=3,
                        deadline=monotonic() + 10,
                        session_store=store,
                    )
                )

        self.assertIn("<persisted-output", batch.outputs[0]["output"])
        self.assertIn("<persisted-output", batch.outputs[1]["output"])
        self.assertEqual(batch.outputs[2]["output"], "C" * 20)
        self.assertEqual(context.read_file_cache, {})

if __name__ == "__main__":
    unittest.main()
