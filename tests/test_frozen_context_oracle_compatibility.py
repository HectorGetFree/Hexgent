"""复用冻结 Phase 4 Oracle，明确隔离新调用协议的测试夹具适配。"""
import unittest
from unittest.mock import patch

from hexgent.context import ContextLimits
from tests.support.fact_extraction_fixture import WithExtraction
import eval.phase4.scenarios as legacy


class FrozenContextOracleCompatibilityTests(unittest.TestCase):
    def test_frozen_phase4_oracle_with_new_protocol_fixture(self):
        original_fake = legacy._FakeResponses
        def limits(**arguments):
            threshold = arguments["context_window_tokens"]-arguments["reserved_output_tokens"]-arguments["safety_margin_tokens"]
            return ContextLimits(**{**arguments, "context_window_tokens": 128000,
                                    "proactive_threshold_tokens": threshold})
        # 仅适配 Fake 协议与可容纳保护区的物理容量；原 verify 一行不改。
        with patch.object(legacy, "_FakeResponses", lambda: WithExtraction(original_fake())), patch.object(legacy, "ContextLimits", limits):
            scenario = legacy.compaction_preserves_complete_tool_round()
            observation = scenario.execute()
            scenario.verify(observation)
