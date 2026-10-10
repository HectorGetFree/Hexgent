"""Eval 包装、预算、来源及完整 Gate 的离线回归。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from eval.pinned_constraints.gate import A, BudgetDenied, EvalClient, run_gate, source_oracle
from eval.support.agent_contracts import text_response


class PinnedGateTests(unittest.TestCase):
    def rows(self):
        return [dict(constraint_id="A",text=A,source_event_seq=1,status="active",superseded_by=None),
                dict(constraint_id="old",text="过期规则",source_event_seq=2,status="superseded",superseded_by="A")]

    def request(self):
        return dict(model="offline",instructions="system",input=[dict(role="user",content="继续")],tools=[],max_output_tokens=100)

    def test_only_active_constraint_is_injected(self):
        client=EvalClient([text_response("完成")],self.rows(),True)
        list(client.responses.create(**self.request()))
        actual=client.model.calls[0]
        self.assertIn(A,actual["instructions"])
        self.assertNotIn("过期规则",actual["instructions"])
        self.assertEqual(actual["input"],self.request()["input"])

    def test_budget_failure_dispatches_no_request(self):
        client=EvalClient([text_response("完成")],self.rows(),True,True)
        with self.assertRaises(BudgetDenied):
            client.responses.create(**self.request())
        self.assertEqual(client.model.calls,[])
        self.assertTrue(client.attempts[0]["denied"])
        self.assertIn(A,client.attempts[0]["block"])

    def test_baseline_pressure_preserves_request(self):
        client=EvalClient([text_response("完成")],self.rows(),False,True)
        list(client.responses.create(**self.request()))
        self.assertEqual(client.model.calls,[self.request()])

    def test_compaction_is_not_wrapped(self):
        client=EvalClient([text_response("摘要")],self.rows(),True,True)
        request={**self.request(),"instructions":"你负责压缩 Agent 的历史会话上下文。"}
        list(client.responses.create(**request))
        self.assertEqual(client.model.calls,[request])

    def test_source_mismatch_is_rejected(self):
        events=[dict(sequence=1,type="context_user_query_appended",payload=dict(content="其他用户请求")),
                dict(sequence=2,type="context_user_query_appended",payload=dict(content="过期规则"))]
        self.assertFalse(source_oracle(self.rows(),events))

    def test_full_gate_preserves_omission_and_native_resume(self):
        with TemporaryDirectory() as temp:
            report=run_gate(Path(temp)/"run")
            self.assertEqual(report["status"],"PASS")
            self.assertTrue(report["production_unchanged"])
            self.assertTrue(report["m1_m2_unchanged"])
            rows={r["id"]:r for r in report["scenarios"]}
            omitted=rows["E2_OMIT"]["variants"]["Baseline"]
            self.assertFalse(omitted["raw_user_constraint_present"])
            self.assertFalse(omitted["summary_retains_constraint"])
            for key in ("E4_A","E4_B"):
                self.assertTrue(rows[key]["variants"]["Pinned"]["oracles"]["resume"])
            self.assertEqual(rows["E5"]["variants"]["Pinned"]["request_count"],0)


if __name__=="__main__":
    unittest.main()
