"""C1-09局部修复回归：真实Production Loop与FakeModel，非正式C1验收。"""

from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import os
import unittest
from unittest.mock import patch

from hexgent import agent_loop
from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig, validate_config
from hexgent.context import ContextManager
from hexgent.loop.loop_state import LoopState
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.utils.token_estimator import estimate_request_tokens
from eval.support.agent_contracts import text_response


class NoSessionRequestBudgetTests(unittest.TestCase):
    """只替换模型响应，不替换Builder、预算计算或工具实现。"""

    def setUp(self):
        self.temp = TemporaryDirectory(prefix="hexgent-budget-repair-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = HexgentConfig()
        self.config = replace(base,
            context=replace(base.context, reserved_output_tokens=100,
                safety_margin_tokens=100, recent_context_tokens=500,
                reactive_recent_context_tokens=200),
            context_extraction=replace(base.context_extraction, enabled=False),
            context_retrieval=replace(base.context_retrieval, enabled=False),
            memory_retrieval=replace(base.memory_retrieval, enabled=False))
        self.evidence = []

    def tearDown(self):
        target = os.environ.get("REQUEST_BUDGET_REPAIR_EVIDENCE_DIR")
        if target:
            path = Path(target) / (self._testMethodName + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.evidence, ensure_ascii=False, indent=2,
                default=lambda v: v.model_dump() if hasattr(v, "model_dump") else vars(v)), encoding="utf-8")

    def run_loop(self, config=None, query="继续", *, retry=False, with_session=False):
        cfg = validate_config(config or self.config)
        calls = []
        estimates = []

        def create(**request):
            calls.append(request)
            if retry and len(calls) == 1:
                return iter([])
            return iter([SimpleNamespace(type="response.completed", response=text_response("完成"))])

        def estimate(*args):
            value = estimate_request_tokens(*args)
            estimates.append(value)
            return value

        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch("hexgent.loop.agent_turn.get_model", return_value="FAKE_ONLY"), \
                patch("hexgent.loop.agent_turn.estimate_request_tokens", side_effect=estimate):
            if with_session:
                engine = AgentEngine(cfg, self.root)
                events = list(engine.run(query, client=client))
                transcript = engine.session.store.read_transcript()
            else:
                manager = ContextManager()
                manager.append_user_query(query)
                context = ToolExecutionContext(InMemoryPlanStore(), self.root)
                events = list(agent_loop(LoopState(manager, context), config=cfg,
                    client=client, session=None))
                transcript = None
        self.evidence.append(dict(config=asdict(cfg), query=query, session=with_session,
            requests=calls, final_budget_estimates=estimates, transcript=transcript,
            terminal=asdict(events[-1].result), provider_actual_usage="NOT_APPLICABLE"))
        return calls, events[-1].result, estimates

    def test_normal_request_dispatches_unchanged(self):
        calls, result, estimates = self.run_loop()
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(estimates), 1)
        request = calls[0]
        self.assertEqual(request["input"], [{"role": "user", "content": "继续"}])
        self.assertEqual(request["max_output_tokens"], 8000)
        self.assertIn("tools", request)
        self.assertTrue(request["stream"])

    def test_original_combined_over_budget_reproduction_has_zero_dispatch(self):
        cfg = replace(self.config, context=replace(self.config.context,
            context_window_tokens=10000, proactive_threshold_tokens=9000))
        calls, result, estimates = self.run_loop(cfg, "x" * 12000)
        self.assertEqual(calls, [])
        self.assertEqual(result.status, "failed")
        self.assertIn("CONTEXT_BUDGET_EXCEEDED", result.output_text)
        self.assertEqual(len(estimates), 1)
        self.assertLess(estimates[0], cfg.context.compaction_threshold_tokens)
        self.assertGreater(estimates[0] + 8000 + 100, 10000)

    def test_tool_schema_is_included_in_final_budget(self):
        calls, _, _ = self.run_loop()
        request = calls[0]
        with_tools = estimate_request_tokens(request["instructions"], request["input"], request["tools"])
        without_tools = estimate_request_tokens(request["instructions"], request["input"], [])
        self.assertGreater(with_tools, without_tools)
        window = without_tools + request["max_output_tokens"] + 100
        cfg = replace(self.config, context=replace(self.config.context, context_window_tokens=window))
        rejected, result, _ = self.run_loop(cfg)
        self.assertEqual(rejected, [])
        self.assertIn("CONTEXT_BUDGET_EXCEEDED", result.output_text)

    def test_output_allowance_and_safety_margin_are_included(self):
        calls, _, _ = self.run_loop()
        request = calls[0]
        estimated = estimate_request_tokens(request["instructions"], request["input"], request["tools"])
        # 输入本身低于压缩阈值；实际输出或余量使最终请求超窗。
        for window in (estimated + 8000 - 1, estimated + 8000 + 99):
            with self.subTest(window=window):
                cfg = replace(self.config, context=replace(self.config.context, context_window_tokens=window))
                rejected, result, _ = self.run_loop(cfg)
                self.assertEqual(rejected, [])
                self.assertIn("CONTEXT_BUDGET_EXCEEDED", result.output_text)

    def test_retry_rechecks_real_budget_before_each_dispatch(self):
        calls, result, estimates = self.run_loop(retry=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(calls), 2)
        # spy只观测真实estimator；每个实际发送前都重新计算，没有伪造返回值。
        self.assertEqual(len(estimates), 2)
        self.assertEqual(calls[0], calls[1])
        for request, actual in zip(calls, estimates):
            self.assertEqual(actual, estimate_request_tokens(request["instructions"], request["input"], request["tools"]))
            self.assertLessEqual(actual + request["max_output_tokens"] + 100,
                self.config.context.context_window_tokens)

    def test_session_normal_and_rejection_contract_is_unchanged(self):
        calls, result, _ = self.run_loop(with_session=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.status, "completed")
        cfg = replace(self.config, loop=replace(self.config.loop, max_output_tokens_per_round=200000))
        calls, result, _ = self.run_loop(cfg, with_session=True)
        self.assertEqual(calls, [])
        self.assertEqual(result.status, "failed")
        self.assertIn("CONTEXT_BUDGET_EXCEEDED", result.output_text)


if __name__ == "__main__":
    unittest.main()
