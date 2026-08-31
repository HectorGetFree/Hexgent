"""连续安全工具的有界并行执行测试。"""

from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, get_ident
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.events import (
    ToolBatchFinishedEvent,
    ToolBatchStartedEvent,
    ToolExecutionCompletedEvent,
    ToolFinishedEvent,
    ToolStartedEvent,
)
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import ToolExecutionResult
from hexgent.tool.tool_scheduler import ToolParallelLimits
from hexgent.tool.tool_registry import get_tool


tool_execution_module = import_module("hexgent.tool.tool_execution")


def drain_generator(generator):
    """收集批次事件并取得生成器最终返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


def make_call(call_id: str, name: str, arguments: str) -> SimpleNamespace:
    """构造最小模型工具调用对象。"""
    return SimpleNamespace(call_id=call_id, name=name, arguments=arguments)


class ToolParallelExecutionTest(unittest.TestCase):
    """验证只并行相邻的注册安全工具。"""

    def test_tool_interrupt_behavior_defaults_to_block(self) -> None:
        """只读工具可取消，写入和 Shell 工具默认等待并保留副作用。"""
        self.assertEqual(get_tool("read_file").interrupt_behavior, "cancel")
        self.assertEqual(get_tool("glob").interrupt_behavior, "cancel")
        self.assertEqual(get_tool("write_file").interrupt_behavior, "block")
        self.assertEqual(get_tool("edit_file").interrupt_behavior, "block")
        self.assertEqual(get_tool("bash").interrupt_behavior, "block")

    def test_partition_preserves_serial_barriers(self) -> None:
        """读工具可以连续成组，写工具必须独占并阻断前后分组。"""
        calls = [
            make_call("c1", "read_file", '{"path":"a.py"}'),
            make_call("c2", "glob", '{"pattern":"*.py"}'),
            make_call(
                "c3",
                "write_file",
                '{"path":"a.py","content":"x"}',
            ),
            make_call("c4", "read_file", '{"path":"b.py"}'),
        ]

        groups = tool_execution_module._partition_tool_calls(calls)

        self.assertEqual(
            [(group.concurrency_safe, [call.call_id for call in group.calls])
             for group in groups],
            [(True, ["c1", "c2"]), (False, ["c3"]), (True, ["c4"])],
        )

    def test_adjacent_read_calls_execute_concurrently_but_commit_in_order(self) -> None:
        """两个相邻读取应真正重叠执行，同时保持事件与结果原始顺序。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(),
                Path(directory).resolve(),
            )
            calls = [
                make_call("c1", "read_file", '{"path":"a.py"}'),
                make_call("c2", "read_file", '{"path":"b.py"}'),
            ]
            barrier = Barrier(2)
            worker_threads: set[int] = set()

            def execute(*_args, **kwargs):
                worker_threads.add(get_ident())
                barrier.wait(timeout=2)
                return ToolExecutionResult(
                    "success",
                    f"结果-{kwargs['call_id']}",
                    False,
                )

            with patch.object(
                tool_execution_module,
                "execute_tool_call",
                side_effect=execute,
            ):
                events, batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        calls,
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=None,
                        deadline=None,
                    )
                )

        self.assertEqual(len(worker_threads), 2)
        self.assertEqual(
            [event.call_id for event in events if isinstance(event, ToolStartedEvent)],
            ["c1", "c2"],
        )
        self.assertEqual(
            [event.call_id for event in events if isinstance(event, ToolFinishedEvent)],
            ["c1", "c2"],
        )
        self.assertEqual(
            [output["call_id"] for output in batch.outputs],
            ["c1", "c2"],
        )
        self.assertEqual(
            [output["output"] for output in batch.outputs],
            ["结果-c1", "结果-c2"],
        )
        batch_started = [
            event for event in events if isinstance(event, ToolBatchStartedEvent)
        ]
        batch_finished = [
            event for event in events if isinstance(event, ToolBatchFinishedEvent)
        ]
        self.assertEqual(len(batch_started), 1)
        self.assertEqual(batch_started[0].tool_count, 2)
        self.assertEqual(batch_started[0].max_workers, 2)
        self.assertEqual(len(batch_finished), 1)
        self.assertEqual(batch_finished[0].succeeded_count, 2)
        self.assertEqual(batch_finished[0].failed_count, 0)
        self.assertGreater(
            events.index(batch_finished[0]),
            max(
                index
                for index, event in enumerate(events)
                if isinstance(event, ToolFinishedEvent)
            ),
        )

    def test_parallel_batch_timeout_returns_paired_errors(self) -> None:
        """并行批次超时后每个调用仍必须得到对应的错误结果。"""
        from time import sleep

        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            calls = [
                make_call("c1", "read_file", '{"path":"a.py"}'),
                make_call("c2", "read_file", '{"path":"b.py"}'),
            ]

            def execute(*_args, **_kwargs):
                sleep(0.05)
                return ToolExecutionResult("success", "迟到结果", False)

            with patch.object(
                tool_execution_module, "execute_tool_call", side_effect=execute
            ):
                events, batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        calls,
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=None,
                        deadline=None,
                        parallel_limits=ToolParallelLimits(
                            batch_timeout_seconds=0.01
                        ),
                    )
                )

        self.assertEqual(
            [output["call_id"] for output in batch.outputs], ["c1", "c2"]
        )
        self.assertTrue(
            all("超过 0.01 秒限制" in output["output"] for output in batch.outputs)
        )
        finished = [
            event for event in events if isinstance(event, ToolBatchFinishedEvent)
        ]
        self.assertEqual(finished[0].failed_count, 2)

    def test_execution_completion_is_immediate_but_model_outputs_stay_ordered(
        self,
    ) -> None:
        """UI 按真实完成顺序收到事件，模型结果仍保持原调用顺序。"""
        from time import sleep

        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )
            calls = [
                make_call("slow", "read_file", '{"path":"slow.py"}'),
                make_call("fast", "read_file", '{"path":"fast.py"}'),
            ]
            fast_finished = Event()

            def execute(*_args, **kwargs):
                if kwargs["call_id"] == "fast":
                    fast_finished.set()
                else:
                    fast_finished.wait(timeout=2)
                    sleep(0.03)
                return ToolExecutionResult(
                    "success", f"结果-{kwargs['call_id']}", False
                )

            with patch.object(
                tool_execution_module, "execute_tool_call", side_effect=execute
            ):
                events, batch = drain_generator(
                    tool_execution_module.execute_tool_batch(
                        calls,
                        context=context,
                        user_confirmation=lambda _tool, _message: "allow",
                        remaining_tool_calls=None,
                        deadline=None,
                    )
                )

        execution_completed = [
            event.call_id
            for event in events
            if isinstance(event, ToolExecutionCompletedEvent)
        ]
        final_finished = [
            event.call_id
            for event in events
            if isinstance(event, ToolFinishedEvent)
        ]
        self.assertEqual(execution_completed, ["fast", "slow"])
        self.assertEqual(final_finished, ["slow", "fast"])
        self.assertEqual(
            [output["call_id"] for output in batch.outputs],
            ["slow", "fast"],
        )

    def test_permission_confirmation_remains_on_scheduler_thread(self) -> None:
        """并行读取工作区外文件时，权限询问仍应在调用线程串行完成。"""
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            calls = [
                make_call("c1", "read_file", '{"path":"../a.py"}'),
                make_call("c2", "read_file", '{"path":"../b.py"}'),
            ]
            scheduler_thread = get_ident()
            confirmation_threads = []
            barrier = Barrier(2)

            def confirm(_tool_name: str, _message: str) -> str:
                confirmation_threads.append(get_ident())
                return "allow"

            def execute(*_args, **_kwargs):
                barrier.wait(timeout=2)
                return ToolExecutionResult("success", "ok", False)

            with patch.object(
                tool_execution_module,
                "execute_tool_call",
                side_effect=execute,
            ):
                drain_generator(
                    tool_execution_module.execute_tool_batch(
                        calls,
                        context=context,
                        user_confirmation=confirm,
                        remaining_tool_calls=None,
                        deadline=None,
                    )
                )

        self.assertEqual(
            confirmation_threads,
            [scheduler_thread, scheduler_thread],
        )


if __name__ == "__main__":
    unittest.main()
