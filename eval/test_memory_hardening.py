"""Memory 评测基础设施离线回归；这些结果不是模型能力数据。"""

from hashlib import sha256
import json
import shutil
from importlib import import_module
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eval.memory_hardening import CASES, _session, analyze, analyze_all, evaluate, prepare
from eval.support.agent_contracts import FakeModel, text_response, tool_call
from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import load_config
from hexgent.context.context_builder import build_context_details
from hexgent.loop.call_model import call_model
from hexgent.memory import RetrievedMemory, RetrievedContextRecord
from hexgent.observation import RunObservation, current_observation
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext


class MemoryReceiptTests(unittest.TestCase):
    def test_receipt_matches_final_selected_content(self):
        with TemporaryDirectory() as directory:
            memory = RetrievedMemory("mem-test", "项目约定", "project", 1.0, ())
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            item = context.memory_store.upsert(memory.content, "project", source="explicit").item
            memory = RetrievedMemory(item.memory_id, item.content, item.category, 1.0, ())
            built = build_context_details(context, relevant_memory=(memory,))
            self.assertIn(memory.content, built.instructions)
            self.assertEqual(built.memory_receipt[0]["content_sha256"], sha256(memory.content.encode()).hexdigest())
            self.assertNotIn("content", built.memory_receipt[0])

    def test_budget_dropped_memory_has_no_receipt(self):
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            memory = RetrievedMemory("mem-test", "项目约定", "project", 1.0, ())
            built = build_context_details(context, relevant_memory=(memory,), runtime_injection_tokens=1)
            self.assertEqual(built.memory_receipt, ())

    def test_context_duplicate_has_no_memory_receipt(self):
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            memory = RetrievedMemory("mem-test", "项目约定", "project", 1.0, ())
            record = RetrievedContextRecord("ctx-test", "项目约定", "fact", (1,), 1.0, ())
            built = build_context_details(context, (record,), (memory,))
            self.assertEqual(built.memory_receipt, ())

    def test_receipt_is_correlated_and_not_sent_to_sdk(self):
        with TemporaryDirectory() as directory:
            requests = []
            def create(**kwargs):
                requests.append(kwargs)
                return iter([SimpleNamespace(type="response.completed", response=SimpleNamespace(output_text="ok"))])
            sink = RunObservation(SimpleNamespace(session_dir=Path(directory)))
            token = current_observation.set(sink)
            try:
                list(call_model(SimpleNamespace(responses=SimpleNamespace(create=create)),
                                model="fake", instructions="最终指令", input=[],
                                observation_memory_receipt=({"memory_id": "mem-test"},)))
            finally:
                current_observation.reset(token)
            self.assertNotIn("observation_memory_receipt", requests[0])
            rows = [json.loads(line) for line in (Path(directory) / "observation.jsonl").read_text().splitlines()]
            receipt = next(r for r in rows if r["event"] == "memory_injected")
            self.assertEqual(receipt["span_id"], rows[0]["span_id"])
            self.assertEqual(receipt["instructions_sha256"], sha256(requests[0]["instructions"].encode()).hexdigest())


class MemoryOracleTests(unittest.TestCase):
    def fixture(self, name="project"):
        case = CASES[name]
        item = SimpleNamespace(memory_id="mem-test", source="explicit", category=case["category"],
                               content=case["a"])
        rows = [{"type": "tool_execution_finished", "payload": {
            "tool_name": "remember", "status": "success", "data": {"memory_id": item.memory_id}}}]
        obs = [{"event": "provider_finished", "span_id": "span", "status": "SUCCEEDED"},
               {"event": "memory_injected", "span_id": "span", "request_type": "MAIN_AGENT",
                "memories": [{"memory_id": item.memory_id,
                              "content_sha256": sha256(item.content.encode()).hexdigest()}]}]
        for index, row in enumerate(obs):
            row.update(execution_id="fixture", monotonic_ns=index)
        terminal = {"status": "completed", "output_text": json.dumps(case["expected"], ensure_ascii=False)}
        return case, rows, obs, item, terminal

    def test_all_three_behavior_oracles(self):
        for name in CASES:
            case, rows, obs, item, terminal = self.fixture(name)
            self.assertEqual(evaluate(case, rows, [], [], obs, [item], terminal, terminal)["status"], "PASS")

    def test_missing_actual_request_receipt_is_blocked_not_pass(self):
        case, rows, obs, item, terminal = self.fixture()
        result = evaluate(case, rows, [], [], [], [item], terminal, terminal)
        self.assertEqual(result["injection_oracle"], "BLOCKED")
        self.assertEqual(result["status"], "NOT_PASS")

    def test_failed_request_does_not_prove_injection(self):
        case, rows, obs, item, terminal = self.fixture()
        obs[0]["status"] = "FAILED"
        self.assertEqual(evaluate(case, rows, [], [], obs, [item], terminal, terminal)["injection_oracle"], "BLOCKED")

    def test_changed_memory_body_does_not_pass_hash_oracle(self):
        case, rows, obs, item, terminal = self.fixture()
        obs[1]["memories"][0]["content_sha256"] = "changed"
        self.assertEqual(evaluate(case, rows, [], [], obs, [item], terminal, terminal)["injection_oracle"], "FAIL")

    def test_wrong_behavior_is_not_hidden_by_successful_injection(self):
        case, rows, obs, item, terminal = self.fixture()
        terminal["output_text"] = '{"project":"北辰","steps":["上线"]}'
        result = evaluate(case, rows, [], [], obs, [item], terminal, terminal)
        self.assertEqual(result["injection_oracle"], "PASS")
        self.assertEqual(result["behavior_oracle"], "FAIL")

    def test_reversed_constraint_is_not_valid_persistence(self):
        case, rows, obs, item, terminal = self.fixture("constraint")
        item.content = "青禾项目使用工单评论区，也允许邮件通知。"
        self.assertEqual(evaluate(case, rows, [], [], obs, [item], terminal, terminal)["persistence_oracle"], "FAIL")

    def test_tool_read_confounds_behavior_evidence(self):
        case, rows, obs, item, terminal = self.fixture()
        reads = [{"type": "tool_execution_finished", "payload": {"tool_name": "read_file", "status": "success"}}]
        result = evaluate(case, rows, reads, [], obs, [item], terminal, terminal)
        self.assertEqual(result["status"], "NOT_PASS")
        self.assertFalse(result["behavior_isolated_from_tool_reads"])

    def test_prepare_isolated_blank_workspaces_and_preserves_existing_artifacts(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with redirect_stdout(StringIO()):
                prepare(output)
            manifest = json.loads((output / "manifest.json").read_text())
            root = Path(manifest["workspace_root"])
            # 只清理本测试创建的目录；prepare 正式产物不会自动删除。
            self.addCleanup(shutil.rmtree, root)
            self.assertEqual(len(manifest["cases"]), 3)
            for case in manifest["cases"].values():
                workspace = Path(case["workspace"])
                self.assertTrue((workspace / "pyproject.toml").is_file())
                self.assertFalse((workspace / ".hexgent").exists())
                self.assertFalse((workspace / "manifest.json").exists())
            with self.assertRaises(ValueError):
                prepare(output)

    def test_session_path_cannot_use_other_workspace(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "Session 不属于"):
                _session(Path(directory) / "other", Path(directory) / "workspace", "query")

    def test_production_loop_artifacts_are_consumable_by_offline_analyzer(self):
        """仅 FakeModel 验证完整工具/注入/Artifact 协议，不产出真实模型指标。"""
        with TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with redirect_stdout(StringIO()):
                prepare(output)
            manifest = json.loads((output / "manifest.json").read_text())
            self.addCleanup(shutil.rmtree, Path(manifest["workspace_root"]))
            loop = import_module("hexgent.loop.agent_loop")
            turn = import_module("hexgent.loop.agent_turn")
            for name, case in manifest["cases"].items():
                workspace = Path(case["workspace"])
                config = load_config(workspace)
                a_model = FakeModel([
                    text_response(output=[tool_call("remember-1", "remember", json.dumps({
                        "content": case["a"], "category": case["category"],
                        "keywords": [case["facts"][0]],
                    }, ensure_ascii=False))]),
                    text_response("已记住"),
                ])
                engine_a = AgentEngine.create(config, workspace)
                with patch.object(loop, "get_client", return_value=SimpleNamespace(responses=a_model, base_url="https://fake.invalid")), patch.object(turn, "get_model", return_value="fake"):
                    list(engine_a.run(RunRequest.submit(case["a"])))
                b_model = FakeModel([text_response(json.dumps(case["expected"], ensure_ascii=False))])
                engine_b = AgentEngine.create(config, workspace)
                with patch.object(loop, "get_client", return_value=SimpleNamespace(responses=b_model, base_url="https://fake.invalid")), patch.object(turn, "get_model", return_value="fake"):
                    list(engine_b.run(RunRequest.submit(case["b"])))
                self.assertIn(case["facts"][0], b_model.calls[0]["instructions"])
                with redirect_stdout(StringIO()):
                    analyze(output, name, engine_a.session.store.session_dir, engine_b.session.store.session_dir)
                report = json.loads(next(output.glob(f"{name}-report-*.json")).read_text())
                self.assertEqual(report["status"], "PASS", report)
            with redirect_stdout(StringIO()):
                summary = analyze_all(output)
            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(summary["persistence_oracle"]["planned"], 3)

    def test_missing_sessions_are_not_dropped_from_summary_denominator(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "run"
            with redirect_stdout(StringIO()):
                prepare(output)
                summary = analyze_all(output)
            manifest = json.loads((output / "manifest.json").read_text())
            self.addCleanup(shutil.rmtree, Path(manifest["workspace_root"]))
            self.assertEqual(summary["blocked_scenarios"], 3)
            self.assertEqual(summary["persistence_oracle"]["planned"], 3)
            self.assertEqual(summary["status"], "NOT_PASS")


if __name__ == "__main__":
    unittest.main()
