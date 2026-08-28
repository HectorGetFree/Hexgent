"""重复工具调用去重测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.events import ToolFinishedEvent
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_batch, execute_tool_call


def drain_generator(generator):
    """收集生成器事件，并取得工具批次最终返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


def make_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """创建测试所需的模型工具调用。"""
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


class ToolDeduplicationTest(unittest.TestCase):
    """验证工具注册策略、调用指纹和缓存失效边界。"""

    def test_exact_glob_call_returns_lightweight_duplicate_result(self) -> None:
        """相同 glob 参数应只真实执行一次，并为新 call_id 返回配对结果。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            calls = [
                make_call("call-1", "glob", '{"pattern":"*.py"}'),
                make_call("call-2", "glob", '{ "pattern" : "*.py" }'),
            ]

            events, batch = drain_generator(
                execute_tool_batch(
                    calls,
                    context=context,
                    user_confirmation=lambda _tool, _message: "allow",
                    remaining_tool_calls=10,
                    deadline=None,
                )
            )

        finished = [event for event in events if isinstance(event, ToolFinishedEvent)]
        self.assertEqual([event.status for event in finished], ["success", "deduplicated"])
        self.assertEqual([output["call_id"] for output in batch.outputs], ["call-1", "call-2"])
        self.assertIn("a.py", batch.outputs[0]["output"])
        self.assertIn("<duplicate-tool-call", batch.outputs[1]["output"])
        self.assertIn('source_call_id="call-1"', batch.outputs[1]["output"])
        self.assertEqual(batch.budgeted_call_count, 2)

    def test_duplicate_limit_returns_error_without_breaking_result_pairing(self) -> None:
        """同一指纹重复达到上限后应返回配对错误，不能无限复用。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            calls = [
                make_call(f"call-{index}", "glob", '{"pattern":"*.py"}')
                for index in range(1, 5)
            ]

            events, batch = drain_generator(
                execute_tool_batch(
                    calls,
                    context=context,
                    user_confirmation=lambda _tool, _message: "allow",
                    remaining_tool_calls=10,
                    deadline=None,
                )
            )

        self.assertEqual(len(batch.outputs), 4)
        self.assertIn("重复调用已达到上限", batch.outputs[-1]["output"])
        self.assertEqual(batch.budgeted_call_count, 4)
        finished = [
            event for event in events if isinstance(event, ToolFinishedEvent)
        ]
        self.assertEqual(finished[-1].status, "deduplicated")

    def test_side_effect_tool_invalidates_exact_call_cache(self) -> None:
        """写入工具成功后，之前缓存的 glob 结果必须失效。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.py").write_text("", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root)

            first = execute_tool_call(
                "glob",
                '{"pattern":"*.py"}',
                context=context,
                call_id="glob-1",
            )
            duplicate = execute_tool_call(
                "glob",
                '{"pattern":"*.py"}',
                context=context,
                call_id="glob-2",
            )
            write = execute_tool_call(
                "write_file",
                '{"path":"b.py","content":""}',
                context=context,
                call_id="write-1",
            )
            refreshed = execute_tool_call(
                "glob",
                '{"pattern":"*.py"}',
                context=context,
                call_id="glob-3",
            )

        self.assertEqual(first.status, "success")
        self.assertEqual(duplicate.status, "deduplicated")
        self.assertEqual(write.status, "success")
        self.assertEqual(refreshed.status, "success")
        self.assertIn("b.py", refreshed.output)

    def test_write_file_is_never_deduplicated(self) -> None:
        """相同副作用工具及参数必须保持每次真实执行。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            arguments = '{"path":"a.txt","content":"内容"}'

            first = execute_tool_call(
                "write_file",
                arguments,
                context=context,
                call_id="write-1",
            )
            second = execute_tool_call(
                "write_file",
                arguments,
                context=context,
                call_id="write-2",
            )

        self.assertEqual(first.status, "success")
        self.assertEqual(second.status, "success")


if __name__ == "__main__":
    unittest.main()
