"""HexgentConfig、AgentEngine 和 LoopState 的配置边界测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import ConfigError, HexgentConfig, RunOptions
from hexgent.context import ContextLimits
from hexgent.events import AgentLoopResult, TextDeltaEvent
from hexgent.loop.loop_state import LoopState
from hexgent.loop.transitions import Stop
from hexgent.loop.limits import LoopLimits
from hexgent.plan import Plan, PlanStep
from hexgent.session import Session, SessionSettings
from hexgent.tool.tool_scheduler import ToolParallelLimits


class AgentEngineConfigTest(unittest.TestCase):
    """验证用户覆盖项只在 Engine 入口消费，LoopState 保持纯活动状态。"""

    def test_run_options_only_override_explicit_sections(self) -> None:
        """RunOptions 应覆盖声明段，其余配置继续继承 HexgentConfig。"""
        process_config = HexgentConfig(
            loop=LoopLimits(max_model_rounds=20),
            context=ContextLimits(context_window_tokens=80_000),
        )
        override = LoopLimits(max_model_rounds=3)

        with TemporaryDirectory() as directory:
            engine = AgentEngine(
                process_config,
                Path(directory),
                session=Session(settings=SessionSettings("accept_edits")),
            )
            resolved = engine.resolve_config(RunOptions(loop=override))

        self.assertIs(resolved.loop, override)
        self.assertIs(resolved.context, process_config.context)
        self.assertIs(resolved.tool_parallel, process_config.tool_parallel)
        self.assertEqual(resolved.permission.default_mode, "default")
        self.assertEqual(engine.session.settings.permission_mode, "accept_edits")

    def test_agent_engine_separates_config_dependencies_and_state(self) -> None:
        """Engine 应显式传递配置与依赖，并保持 LoopState 为纯活动状态。"""
        with TemporaryDirectory() as directory:
            engine = AgentEngine(
                HexgentConfig(loop=LoopLimits(max_model_rounds=20)),
                Path(directory),
                session=Session(settings=SessionSettings("plan")),
            )
            override = LoopLimits(max_model_rounds=2)

            with patch(
                "hexgent.agent_engine.agent_loop",
                return_value=iter(()),
            ) as loop:
                events = engine.run("检查项目", options=RunOptions(loop=override))
                self.assertEqual(list(events), [])

            (state,) = loop.call_args.args
            effective_config = loop.call_args.kwargs["config"]
            self.assertIs(effective_config.loop, override)
            self.assertEqual(effective_config.permission.default_mode, "default")
            self.assertEqual(engine.session.settings.permission_mode, "plan")
            self.assertIs(state.context_manager, engine.context_manager)
            self.assertIs(state.tool_context, engine.tool_context)
            self.assertIs(loop.call_args.kwargs["session"], engine.session)
            self.assertFalse(
                loop.call_args.kwargs[
                    "run_control"
                ].interrupt_controller.interrupted
            )
            self.assertFalse(hasattr(state, "config"))
            self.assertEqual(
                [query.content for query in engine.context_manager.user_queries],
                ["检查项目"],
            )

    def test_unconsumed_stream_does_not_append_query(self) -> None:
        """只创建事件流而不消费时，不应提前修改共享 Context。"""
        with TemporaryDirectory() as directory:
            engine = AgentEngine(HexgentConfig(), Path(directory), session=Session())
            stream = engine.run("尚未开始的任务")

            self.assertEqual(engine.context_manager.user_queries, ())
            stream.close()

    def test_concurrent_run_is_rejected_before_appending_query(self) -> None:
        """同一 Engine 的第二个 Run 不得把 Query 注入正在执行的 Run。"""
        with TemporaryDirectory() as directory:
            engine = AgentEngine(HexgentConfig(), Path(directory), session=Session())
            with patch(
                "hexgent.agent_engine.agent_loop",
                return_value=iter([TextDeltaEvent("处理中")]),
            ):
                first = engine.run("任务 A")
                self.assertEqual(next(first).delta, "处理中")

                second = engine.run("任务 B")
                with self.assertRaisesRegex(RuntimeError, "已有正在执行"):
                    next(second)

                self.assertEqual(
                    [query.content for query in engine.context_manager.user_queries],
                    ["任务 A"],
                )
                first.close()

    def test_invalid_base_and_run_override_configs_are_rejected(self) -> None:
        """代码构造的基础配置和单次覆盖都必须经过统一校验。"""
        invalid_parallel = ToolParallelLimits(max_workers=0)
        with TemporaryDirectory() as directory:
            with self.assertRaises(ConfigError):
                AgentEngine(
                    HexgentConfig(tool_parallel=invalid_parallel),
                    Path(directory),
                )

            engine = AgentEngine(HexgentConfig(), Path(directory), session=Session())
            with self.assertRaises(ConfigError):
                engine.resolve_config(RunOptions(tool_parallel=invalid_parallel))

            with self.assertRaises(ConfigError):
                engine.resolve_config(RunOptions(loop="错误配置"))  # type: ignore[arg-type]

    def test_cancel_pending_task_clears_retained_plan(self) -> None:
        """暂停后的业务取消应清除 Plan，但不需要伪造一个新的 Agent Run。"""
        with TemporaryDirectory() as directory:
            engine = AgentEngine(HexgentConfig(), Path(directory), session=Session())
            engine.tool_context.plan_store.set_plan(
                Plan(
                    "旧任务",
                    "in_progress",
                    [
                        PlanStep(
                            "继续执行",
                            "正在继续执行",
                            "in_progress",
                            "step-1",
                        )
                    ],
                )
            )

            engine.cancel_pending_task()

            self.assertIsNone(engine.tool_context.plan_store.get_plan())

    def test_continue_reuses_checkpoint_state_without_appending_query(self) -> None:
        """continue 应继承预算状态，且不能重复写入用户 Query。"""
        with TemporaryDirectory() as directory:
            engine = AgentEngine(HexgentConfig(), Path(directory), session=Session())
            first_state = LoopState(
                context_manager=engine.context_manager,
                tool_context=engine.tool_context,
                model_rounds=3,
                tool_calls=5,
            )
            engine._pending_run_state = first_state.to_snapshot()

            def stopped_loop(state: LoopState, **_: object):
                if False:
                    yield TextDeltaEvent("")
                return Stop(
                    AgentLoopResult("", state.elapsed_seconds, status="completed"),
                    state,
                )

            with patch("hexgent.agent_engine.agent_loop", side_effect=stopped_loop) as loop:
                self.assertEqual(list(engine.run(RunRequest.continue_run())), [])

            (restored_state,) = loop.call_args.args
            self.assertEqual(restored_state.model_rounds, 3)
            self.assertEqual(restored_state.tool_calls, 5)
            self.assertEqual(engine.context_manager.user_queries, ())
            self.assertIsNone(engine._pending_run_state)

    def test_submit_supersedes_pending_checkpoint_run(self) -> None:
        """新 Query 必须取代旧快照，不能隐式拼接为 continue。"""
        with TemporaryDirectory() as directory:
            engine = AgentEngine(HexgentConfig(), Path(directory), session=Session())
            state = LoopState(engine.context_manager, engine.tool_context)
            engine._pending_run_state = state.to_snapshot()

            with patch("hexgent.agent_engine.agent_loop", return_value=iter(())):
                self.assertEqual(list(engine.run(RunRequest.submit("新的方向"))), [])

            self.assertEqual(
                [query.content for query in engine.context_manager.user_queries],
                ["新的方向"],
            )
            self.assertIsNone(engine._pending_run_state)


if __name__ == "__main__":
    unittest.main()
