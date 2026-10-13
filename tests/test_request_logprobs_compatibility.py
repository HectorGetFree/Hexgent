"""真实 ContextManager 请求边界回归；不改历史Fixture。"""
from copy import deepcopy
import unittest
from types import SimpleNamespace

from hexgent.context import ContextManager


class RequestLogprobsCompatibilityTests(unittest.TestCase):
    def test_null_logprobs_normalized_without_history_mutation(self):
        raw = dict(type="message", role="assistant", content=[dict(type="output_text", text="原文事实", annotations=[], logprobs=None)])
        before = deepcopy(raw)
        manager = ContextManager()
        manager.append_messages([raw], completes_round=True)
        materialized = manager.materialize_messages(manager.active_messages)
        self.assertEqual(materialized[0]["content"][0]["logprobs"], [])
        self.assertEqual(raw, before)
        self.assertEqual(manager.active_messages[0], before)
        self.assertEqual(materialized[0]["content"][0]["text"], "原文事实")

    def test_nonempty_logprobs_and_tool_text_preserved(self):
        raw = dict(role="assistant", content=[dict(type="output_text", text="事实", logprobs=[dict(token="事实", logprob=-1)])])
        tool = dict(type="function_call_output", call_id="c1", output='{"logprobs":null}')
        manager = ContextManager()
        self.assertEqual(manager.materialize_messages([raw, tool]), [raw, tool])

    def test_sdk_and_restored_dict_requests_are_equal(self):
        raw = dict(type="message", role="assistant", content=[dict(type="output_text", text="事实", logprobs=None)])
        sdk = SimpleNamespace(model_dump=lambda **kwargs: deepcopy(raw))
        manager = ContextManager()
        self.assertEqual(manager.materialize_messages([sdk]), manager.materialize_messages([raw]))

    def test_summary_and_main_share_materialization(self):
        manager = ContextManager()
        manager.append_user_query("继续")
        raw = dict(role="assistant", content=[dict(type="output_text", text="保留", logprobs=None)])
        manager.append_messages([raw], completes_round=True)
        self.assertEqual(manager.model_messages(), manager.materialize_messages(manager.active_messages))
        self.assertIsNone(raw["content"][0]["logprobs"])


if __name__ == "__main__":
    unittest.main()
