"""会话 Transcript 和大工具结果持久化测试。"""

import json
from dataclasses import FrozenInstanceError
from hashlib import sha256
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
from hexgent.tool.tool_context import ReadFileCacheEntry, ReadFileCacheKey
from hexgent.tool.tool_execution import ToolExecutionResult
from hexgent.tool.tool_execution import execute_tool_call


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
            r"^\d{8}-\d{6}_原始 Query：不要修改空格 和标_[0-9a-f]{6}$",
        )
        self.assertLessEqual(len(session.store.session_dir.name), 42)
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
                (
                    "read_file_test_agent_loop_limits.py__"
                    f"{sha256(output.encode('utf-8')).hexdigest()[:12]}.txt"
                ),
            )
            self.assertIn("path=", message)
            self.assertIn("01234", message)
            self.assertNotIn(output, message)

    def test_identical_large_results_reuse_one_artifact(self) -> None:
        """同一 Session 中的相同大结果应复用同一物理工件。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root, session_id="session-reuse")
            output = "重复的大结果" * 100

            first = store.persist_tool_result(
                "call-1",
                "read_file",
                output,
                force=True,
                description="read_file(/tmp/first.log)",
            )
            second = store.persist_tool_result(
                "call-2",
                "read_file",
                output,
                force=True,
                description="read_file(/tmp/second.log)",
            )
            records = [
                json.loads(line)
                for line in store.transcript_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            result_paths = list(store.tool_results_dir.glob("*.txt"))

        self.assertEqual(first.relative_path, second.relative_path)
        self.assertFalse(first.reused)
        self.assertTrue(second.reused)
        self.assertEqual(len(result_paths), 1)
        persisted_records = [
            record for record in records if record["type"] == "tool_result_persisted"
        ]
        self.assertEqual(
            [record["payload"]["call_id"] for record in persisted_records],
            ["call-1", "call-2"],
        )
        self.assertEqual(
            [record["payload"]["reused"] for record in persisted_records],
            [False, True],
        )

    def test_tool_batch_reuses_artifact_after_context_cache_clear(self) -> None:
        """工具批次清空 Context 缓存后仍应复用 Session 工件。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(
                root,
                session_id="session-cache-clear",
                tool_result_limits=ToolResultLimits(max_inline_characters=10),
            )
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            context.read_file_cache[
                ReadFileCacheKey(root / "large.log")
            ] = ReadFileCacheEntry("cached-hash")
            first_call = SimpleNamespace(
                call_id="call-1",
                name="read_file",
                arguments='{"path":"large.log"}',
            )
            second_call = SimpleNamespace(
                call_id="call-2",
                name="read_file",
                arguments='{"path":"large.log"}',
            )
            large_output = "相同的大结果" * 100

            with patch.object(
                tool_execution_module,
                "execute_tool_call",
                return_value=ToolExecutionResult(
                    "success", large_output, False
                ),
            ):
                _, first_batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        [first_call],
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=1,
                        deadline=monotonic() + 10,
                        session_store=store,
                    )
                )
                self.assertEqual(context.read_file_cache, {})
                _, second_batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        [second_call],
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=1,
                        deadline=monotonic() + 10,
                        session_store=store,
                    )
                )
            result_paths = list(store.tool_results_dir.glob("*.txt"))
            records = [
                json.loads(line)
                for line in store.transcript_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]

        self.assertEqual(
            first_batch.outputs[0]["output"],
            second_batch.outputs[0]["output"],
        )
        self.assertEqual(len(result_paths), 1)
        persisted_records = [
            record for record in records if record["type"] == "tool_result_persisted"
        ]
        self.assertEqual(
            [record["payload"]["reused"] for record in persisted_records],
            [False, True],
        )

    def test_recreated_store_reuses_content_addressed_artifact(self) -> None:
        """重建 SessionStore 后也应从磁盘识别并复用已有工件。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = "能够跨 Store 恢复的结果" * 100
            first_store = SessionStore(root, session_id="session-resume")
            first = first_store.persist_tool_result(
                "call-1",
                "read_file",
                output,
                force=True,
                description="read_file(original.log)",
            )

            resumed_store = SessionStore(root, session_id="session-resume")
            resumed = resumed_store.persist_tool_result(
                "call-2",
                "read_file",
                output,
                force=True,
                description="read_file(copy.log)",
            )
            result_paths = list(resumed_store.tool_results_dir.glob("*.txt"))

        self.assertEqual(first.relative_path, resumed.relative_path)
        self.assertTrue(resumed.reused)
        self.assertEqual(len(result_paths), 1)

    def test_persisted_tool_result_can_be_restored_by_line_range(self) -> None:
        """模型应能通过外置路径只恢复大结果中需要的行范围。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = SessionStore(root, session_id="session-restore")
            persisted = store.persist_tool_result(
                "call-restore",
                "read_file",
                "第一行\n第二行\n第三行\n第四行\n第五行",
                force=True,
                description="read_file(large.txt)",
            )
            context = ToolExecutionContext(InMemoryPlanStore(), root)

            result = execute_tool_call(
                "read_file",
                json.dumps(
                    {
                        "path": persisted.relative_path,
                        "start_line": 3,
                        "line_count": 2,
                    },
                    ensure_ascii=False,
                ),
                context=context,
            )

        self.assertEqual(result.status, "success")
        self.assertIn('lines="3-4"', result.output)
        self.assertIn("第三行\n第四行", result.output)
        self.assertNotIn("第一行", result.output)
        self.assertNotIn("第五行", result.output)

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
            context.read_file_cache[
                ReadFileCacheKey(root / "example.txt")
            ] = ReadFileCacheEntry("cached-hash")
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
                f"bash_example__{sha256(('A' * 20).encode()).hexdigest()[:12]}.txt",
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
                (
                    ".hexgent/sessions/session-batch/tool-results/"
                    f"bash_example__{sha256(('A' * 20).encode()).hexdigest()[:12]}.txt"
                ),
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
            context.read_file_cache[
                ReadFileCacheKey(root / "example.txt")
            ] = ReadFileCacheEntry("cached-hash")
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
