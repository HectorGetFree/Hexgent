"""Phase5C-1 不联网可行性验证。"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.memory import ContextRecordInput
from hexgent.memory.context_record_extraction import ContextExtractionLimits
from hexgent.memory.context_retrieval import ContextRetrievalLimits
from hexgent.memory.memory_prompts import CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS

from eval.phase5c.fixtures import baseline_path, cloned_snapshot, cloned_workspace, freeze_snapshot, workspace_manifest
from eval.phase5c.artifacts import FeasibilityArtifact, write_artifact
from eval.phase5c.l1 import freeze_qualified_stage_a, qualify_stage_a_record
from eval.phase5c.observation import project_transcript
from eval.phase5c.oracles import evaluate
from eval.phase5c.specifications import FEASIBILITY_TASKS
from eval.support.agent_contracts import text_response


class _StageAExtractionClient:
    """只为 L1 本地资格化测试提供确定性主响应和提取响应。"""

    def __init__(self) -> None:
        self.responses = self
        self.calls: list[dict[str, object]] = []

    def create(self, **request: object):
        self.calls.append(request)
        instructions = str(request.get("instructions", ""))
        if instructions == CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS:
            payload = json.loads(str(request["input"][0]["content"]))
            source = next(
                fact for fact in payload["facts"]
                if fact["type"] == "context_user_query_appended"
            )
            candidate = {
                "records": [{
                    "content": "用户表示发布策略的权威来源是 config/release-policy.json。",
                    "category": "fact",
                    "source_sequences": [source["sequence"]],
                    "supersedes": [],
                    "evidence_quote": "分析发布资料，权威策略位于 config/release-policy.json。",
                }],
            }
            response = text_response(json.dumps(candidate, ensure_ascii=False))
        else:
            response = text_response("已完成发布资料分析。")
        events = [
            SimpleNamespace(type="response.output_text.delta", delta=delta)
            for delta in response.text_deltas
        ]
        events.append(SimpleNamespace(type="response.completed", response=response))
        return iter(events)


class Phase5CFeasibilityTest(unittest.TestCase):
    def test_task_contracts_cover_six_distinct_feasibility_tasks(self) -> None:
        self.assertEqual({task.task_id for task in FEASIBILITY_TASKS}, {"R1", "D2", "W1", "C1", "T1", "L1"})
        self.assertEqual(len({task.fixture for task in FEASIBILITY_TASKS}), 4)
        self.assertTrue(all(task.required_tools and task.oracle for task in FEASIBILITY_TASKS))

    def test_clone_isolated_and_baseline_unchanged(self) -> None:
        baseline = workspace_manifest(baseline_path("research-synthesis"))
        with cloned_workspace("research-synthesis") as first, cloned_workspace("research-synthesis") as second:
            (first / "brief.md").write_text("temporary", encoding="utf-8")
            self.assertFalse((second / "brief.md").exists())
        self.assertEqual(workspace_manifest(baseline_path("research-synthesis")), baseline)

    def test_oracles_accept_valid_outputs_and_reject_forbidden_changes(self) -> None:
        with cloned_workspace("document-operations") as workspace:
            before = workspace_manifest(workspace)
            (workspace / "review-result.json").write_text(json.dumps({"reviews": [
                {"name": "alice", "decision": "approved", "reason": "score_at_least_80"},
                {"name": "bob", "decision": "rejected", "reason": "score_below_80"},
            ]}), encoding="utf-8")
            self.assertEqual(evaluate("D2", workspace, before).state, "ORACLE_PASS")
            (workspace / "policy.json").write_text("{}", encoding="utf-8")
            self.assertEqual(evaluate("D2", workspace, before).state, "ORACLE_FAIL")

    def test_technical_oracle_requires_real_fixture_test_and_allowed_diff(self) -> None:
        with cloned_workspace("technical-mini-service") as workspace:
            before = workspace_manifest(workspace)
            (workspace / "app/parser.py").write_text("def normalize_label(value: str) -> str:\n    return value.strip().lower()\n", encoding="utf-8")
            self.assertEqual(evaluate("T1", workspace, before).state, "ORACLE_PASS")
            (workspace / "README.md").write_text("unexpected", encoding="utf-8")
            self.assertEqual(evaluate("T1", workspace, before).state, "ORACLE_FAIL")

    def test_observation_projects_permission_dispatch_and_failure_by_call_id(self) -> None:
        records = [
            {"type": "tool_execution_started", "payload": {"call_id": "c1", "tool_name": "write_file"}},
            {"type": "permission_resolved", "payload": {"call_id": "c1", "tool_name": "write_file", "behavior": "allow"}},
            {"type": "tool_execution_dispatched", "payload": {"call_id": "c1", "tool_name": "write_file"}},
            {"type": "tool_execution_finished", "payload": {"call_id": "c1", "tool_name": "write_file", "status": "error", "failure": {"code": "business_failure"}}},
            {"type": "agent_run_finished", "payload": {"status": "completed", "stop_reason": None}},
        ]
        projected = project_transcript(records).to_dict()
        self.assertEqual(projected["failed_calls"], 1)
        self.assertEqual(projected["tool_calls"][0]["permission_outcome"], "allow")
        self.assertTrue(projected["tool_calls"][0]["dispatch_occurred"])

    def test_artifact_persists_runtime_diagnostics_diff_and_external_oracle(self) -> None:
        records = [
            {"type": "tool_execution_finished", "payload": {
                "call_id": "c1", "tool_name": "read_file", "status": "success",
            }},
            {"type": "agent_run_finished", "payload": {"status": "completed", "stop_reason": None}},
        ]
        with TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            before = workspace_manifest(workspace)
            (workspace / "brief.md").write_text(
                "每周报告\n来源：market-notes.md、field-interviews.md\n时间冲突", encoding="utf-8",
            )
            artifact = FeasibilityArtifact(
                "R1", True, project_transcript(records), evaluate("R1", workspace, before),
            )
            path = write_artifact(Path(directory) / "artifact.json", artifact)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["runtime"]["tool_calls"][0]["call_id"], "c1")
            self.assertEqual(payload["workspace_diff_summary"], ["brief.md"])
            self.assertEqual(payload["oracle"]["state"], "ORACLE_PASS")

    def test_l1_qualification_and_snapshot_preserve_durable_state_for_paired_clones(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            engine = AgentEngine.create(HexgentConfig(
                context_extraction=ContextExtractionLimits(enabled=True, turn_interval=1),
            ), workspace)
            client = _StageAExtractionClient()
            list(engine.run("分析发布资料，权威策略位于 config/release-policy.json。", client=client))
            transcript = engine.session.store.read_transcript() if engine.session.store else []
            self.assertTrue(any(
                item["type"] == "context_record_extraction_committed" for item in transcript
            ))
            record = engine.session.context_records.active_records[0]
            qualification = qualify_stage_a_record(
                record,
                transcript,
                stage_b_prompt="继续更新交接文档，并依据发布策略来源处理风险。",
                final_answer_markers=("handoff_ready",),
            )
            self.assertTrue(qualification.qualified, qualification.reason)
            snapshot = Path(directory) / "stage-a-snapshot"
            freeze_qualified_stage_a(workspace, snapshot, qualification)
            session_id = engine.session.store.session_id if engine.session.store else None
            self.assertIsNotNone(session_id)
            with cloned_snapshot(snapshot) as on, cloned_snapshot(snapshot) as off:
                self.assertTrue((on / ".hexgent").exists())
                on_engine = AgentEngine.restore(HexgentConfig(
                    context_retrieval=ContextRetrievalLimits(enabled=True),
                ), on, session_id)
                off_engine = AgentEngine.restore(HexgentConfig(
                    context_retrieval=ContextRetrievalLimits(enabled=False),
                ), off, session_id)
                stage_b_prompt = "依据发布策略更新交接文档"
                on_query = on_engine.context_manager.append_user_query(stage_b_prompt)
                on_engine.session.record_user_query(on_query)
                off_query = off_engine.context_manager.append_user_query(stage_b_prompt)
                off_engine.session.record_user_query(off_query)
                on_records = on_engine.session.retrieve_context_records(
                    stage_b_prompt, (on_query.sequence,), on_engine.config.context_retrieval,
                )
                off_records = off_engine.session.retrieve_context_records(
                    stage_b_prompt, (off_query.sequence,), off_engine.config.context_retrieval,
                )
                self.assertEqual([item.record_id for item in on_records], [record.record_id])
                self.assertEqual(off_records, ())
                (on / "stage-b-only.txt").write_text("on", encoding="utf-8")
                self.assertFalse((off / "stage-b-only.txt").exists())

    def test_l1_rejects_answer_leaking_or_untraceable_record(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            engine = AgentEngine.create(HexgentConfig(), workspace)
            query = engine.context_manager.append_user_query("分析发布资料")
            engine.session.record_user_query(query)
            record = engine.session.upsert_context_records([
                ContextRecordInput("发布策略位于配置目录，最终答案是 handoff_ready。", "fact", (1,)),
            ])[0].record
            transcript = engine.session.store.read_transcript() if engine.session.store else []
            result = qualify_stage_a_record(
                record,
                transcript,
                stage_b_prompt="继续依据发布策略更新交接文档",
                final_answer_markers=("handoff_ready",),
            )
            self.assertFalse(result.qualified)
            self.assertIn("contains_stage_b_answer", result.reason)
