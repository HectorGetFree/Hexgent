"""HexgentConfig、AgentEngine 和 LoopState 的配置边界测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig, RunOptions
from hexgent.context import ContextLimits
from hexgent.loop.limits import LoopLimits
from hexgent.session import Session, SessionSettings


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
        self.assertEqual(resolved.permission.default_mode, "accept_edits")

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

            (state,) = loop.call_args.args
            effective_config = loop.call_args.kwargs["config"]
            self.assertEqual(list(events), [])
            self.assertIs(effective_config.loop, override)
            self.assertEqual(effective_config.permission.default_mode, "plan")
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


if __name__ == "__main__":
    unittest.main()
