"""复用冻结 Phase 4 Oracle，明确隔离新调用协议的测试夹具适配。"""
import unittest
from unittest.mock import patch

import eval.phase4.scenarios as legacy


class FrozenContextOracleCompatibilityTests(unittest.TestCase):
    def test_frozen_phase4_oracle_with_new_protocol_fixture(self):
        original_fake = legacy._FakeResponses
        responses = original_fake()
        # 场景自身已适配新协议，不再套用按旧 10-token 窗口推导阈值的二次包装。
        with patch.object(legacy, "_FakeResponses", return_value=responses):
            scenario = legacy.compaction_preserves_complete_tool_round()
            observation = scenario.execute()
            scenario.verify(observation)
        self.assertEqual(len(responses.extraction_calls), 1)
        self.assertEqual(len(responses.calls), 1)
        self.assertNotEqual(responses.extraction_calls[0]["instructions"], responses.calls[0]["instructions"])
