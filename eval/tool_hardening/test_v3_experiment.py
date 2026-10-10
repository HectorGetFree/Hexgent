"""V3 配对统计与无损重放回归；不发起模型请求。"""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .v3_experiment import aggregate, eligible_batches, execution_contract
from .v3_trace_replay import execute, freeze, hashes, verify
from .v2_runner import digest


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def trace(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        for name, text in (("a.txt", "first"), ("b.txt", "second")):
            (workspace / name).write_text(text)
        source = self.root / "original.json"
        source.write_text("original")
        calls = [{"call_id": name, "name": "read_file", "arguments": json.dumps({"path": name + ".txt"})} for name in ("a", "b")]
        expected = [{"call_id": c["call_id"], "tool_name": "read_file", "arguments": c["arguments"], "status": "success",
                     "content_sha256": sha256(text.encode()).hexdigest()} for c, text in zip(calls, ("first", "second"))]
        return {"workspace": str(workspace), "source_artifact": str(source), "source_artifact_sha256": digest(source), "snapshot": hashes(workspace),
                "batches": [{"turn_id": 1, "response_id": "r1", "calls": calls, "expected": expected, "cache_clear_before": False},
                            {"turn_id": 2, "response_id": "r2", "calls": [], "expected": [], "cache_clear_before": False}]}

    def test_real_pipeline_replay_equivalent_and_serial_peak_one(self):
        trace = self.trace()
        before = deepcopy(trace)
        serial = execute(trace, self.root / "serial", False, profile=True)
        parallel = execute(trace, self.root / "parallel", True, profile=True)
        self.assertEqual(serial["normalized_results"], parallel["normalized_results"])
        self.assertEqual(serial["handler_peak_concurrency"], 1)
        self.assertEqual(serial["actual_parallel_batches"], 0)
        self.assertEqual(parallel["actual_parallel_batches"], 1)
        self.assertEqual(len(serial["batches"]), 2)
        self.assertEqual(trace, before)
        self.assertEqual(hashes(Path(trace["workspace"])), trace["snapshot"])

    def test_lossy_replay_stops_and_retains_mismatch(self):
        trace = self.trace()
        trace["batches"][0]["expected"][0]["content_sha256"] = "wrong"
        with self.assertRaisesRegex(RuntimeError, "TRACE_CANNOT_REPLAY_LOSSLESSLY"):
            execute(trace, self.root / "failed", False)
        self.assertTrue((self.root / "failed/mismatch.json").exists())

    def test_permission_decisions_must_match_original(self):
        trace = self.trace()
        trace["permissions"] = {c["call_id"]: [] for c in trace["batches"][0]["calls"]}
        with self.assertRaisesRegex(RuntimeError, "REPLAY_PERMISSION_MISMATCH"):
            execute(trace, self.root / "permission-mismatch", False)
        self.assertTrue((self.root / "permission-mismatch/permission-mismatch.json").exists())

    def test_source_changed_is_not_silently_excluded(self):
        trace = self.trace()
        (Path(trace["workspace"]) / "a.txt").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "REPLAY_SOURCE_CHANGED"):
            verify(trace)

    def test_parallel_opportunity_does_not_merge_responses(self):
        call = {"call_id": "a", "name": "read_file", "arguments": '{"path":"a"}'}
        other = {"call_id": "b", "name": "read_file", "arguments": '{"path":"b"}'}
        self.assertEqual(eligible_batches([{"tool_calls": [call]}, {"tool_calls": [other]}]), 0)
        self.assertEqual(eligible_batches([{"tool_calls": [call, other]}]), 1)
        self.assertEqual(eligible_batches([{"tool_calls": [call, call]}]), 0)

    def test_controlled_error_does_not_become_pipeline_failure(self):
        call = {"call_id": "a", "name": "read_file", "arguments": '{"path":"missing"}'}
        records = [
            {"sequence": 1, "type": "tool_execution_started", "payload": {"call_id": "a", "tool_name": "read_file", "arguments": '{"path":"missing"}'}},
            {"sequence": 2, "type": "permission_resolved", "payload": {"call_id": "a", "behavior": "allow"}},
            {"sequence": 3, "type": "tool_execution_dispatched", "payload": {"call_id": "a"}},
            {"sequence": 4, "type": "tool_execution_finished", "payload": {"call_id": "a", "tool_name": "read_file", "status": "error", "failure": {"code": "file_not_found"}}}]
        turns = [{"request_type": "MAIN_AGENT", "tool_calls": [call]}]
        self.assertEqual(execution_contract(records, turns)["status"], "PASS")
        records[1]["payload"]["behavior"] = "deny"
        self.assertEqual(execution_contract(records, turns)["status"], "FAIL")

    def test_aggregate_semantic_pending_and_complete_pair_only(self):
        def metric(latency):
            return {"total_agent_latency_ms": latency, "main_provider_latency_ms": 1, "auxiliary_provider_latency_ms": 0,
                    "total_tool_execution_latency_ms": 1, "strict_citation_pass": False,
                    "oracles": {"tool_execution": {"status": "PASS"}, "tool_result_delivery": {"status": "PASS"}}}
        rows = [{"task": "tool_pipeline", "runs": {"ON": metric(20), "OFF": metric(10)}},
                {"task": "tool_pipeline", "runs": {"ON": metric(1)}}]
        result = aggregate(rows)["tool_pipeline"]
        self.assertEqual(result["pair_count"], 1)
        self.assertEqual(result["median_off_over_on_agent_latency"], .5)
        self.assertEqual(result["mean_agent_latency_reduction_pct"], -100)
        self.assertIsNone(result["semantic_success_count"])
