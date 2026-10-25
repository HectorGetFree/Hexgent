"""M1 离线隔离与 Oracle 回归，固定模型输出不作为能力指标。"""

from contextlib import redirect_stdout
from dataclasses import asdict
from importlib import import_module
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eval.memory_m1 import CASES, answer_oracle, config_for, execute_stage, no_external_fact_source, pair_valid, prepare, scope_probes, summarize_run
from eval.support.agent_contracts import FakeModel, text_response, tool_call
from hexgent.agent_engine import AgentEngine
from hexgent.memory import MemoryStore
from hexgent.tool.tool_permission_pipeline import PermissionContext, evaluate_permission
from hexgent.tool.tool_registry import get_tool
from hexgent.tool.tool_execution import execute_tool_call


class MemoryM1Tests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "run"
        with redirect_stdout(StringIO()):
            prepare(self.root)
        self.manifest = json.loads((self.root / "manifest.json").read_text())

    def test_six_preregistered_independent_entities_and_alternating_order(self):
        cases = self.manifest["cases"]
        self.assertEqual(len(cases), 6)
        self.assertEqual(len({c["entity"] for c in cases}), 6)
        self.assertEqual([c["order"][0] for c in cases], ["ON", "OFF"] * 3)

    def test_off_changes_only_long_term_memory_enabled(self):
        on, off = asdict(config_for(self.manifest, "ON")), asdict(config_for(self.manifest, "OFF"))
        self.assertTrue(on["memory_retrieval"]["enabled"])
        self.assertFalse(off["memory_retrieval"]["enabled"])
        off["memory_retrieval"]["enabled"] = True
        self.assertEqual(on, off)
        # 非被测模块沿用准备时的配置，不假定 Production 默认仍启用 ContextRecord。
        self.assertEqual(on["context_retrieval"], self.manifest["base_config"]["context_retrieval"])
        self.assertEqual(on["context_extraction"], self.manifest["base_config"]["context_extraction"])

    def test_private_vault_and_write_transcript_access_are_denied(self):
        workspace, vault = self.root / "query", self.root / "vault"
        workspace.mkdir()
        engine = AgentEngine(config_for(self.manifest, "OFF"), workspace, memory_store=MemoryStore(vault))
        scope_probes(engine, vault)
        for path in (vault / ".hexgent/memory/items/mem-test.md", self.root / "WRITE/workspace/.hexgent/sessions/transcript.jsonl"):
            decision = evaluate_permission("read_file", get_tool("read_file"), {"path": str(path)}, engine.tool_context, PermissionContext("dont_ask"))
            self.assertEqual(decision.behavior, "deny")

    def test_symlink_cannot_bypass_native_path_resolution(self):
        workspace, vault = self.root / "query", self.root / "vault"
        workspace.mkdir()
        vault.mkdir()
        (workspace / "link").symlink_to(vault, target_is_directory=True)
        engine = AgentEngine(config_for(self.manifest, "OFF"), workspace, memory_store=MemoryStore(vault))
        decision = evaluate_permission("read_file", get_tool("read_file"), {"path": "link/secret.md"}, engine.tool_context, PermissionContext("dont_ask"))
        self.assertEqual(decision.behavior, "deny")

    def test_production_pipeline_does_not_dispatch_external_read_or_shell(self):
        workspace, vault = self.root / "query", self.root / "vault"
        workspace.mkdir()
        vault.mkdir()
        (vault / "fact.txt").write_text("synthetic-private-answer", encoding="utf-8")
        for variant in ("ON", "OFF"):
            engine = AgentEngine(config_for(self.manifest, variant), workspace, memory_store=MemoryStore(vault))
            entered = []
            for name, arguments in (("read_file", {"path": str(vault / "fact.txt")}),
                                    ("bash", {"command": "cat ../vault/fact.txt"})):
                result = execute_tool_call(name, json.dumps(arguments), context=engine.tool_context,
                    permission_context=PermissionContext("dont_ask"), execution_notifier=entered.append, call_id="probe")
                self.assertEqual(result.status, "denied")
                self.assertNotIn("synthetic-private-answer", result.output)
            self.assertEqual(entered, [])

    def test_same_oracle_for_on_off_and_unknown_is_not_correct(self):
        for case in CASES:
            self.assertTrue(answer_oracle(case, json.dumps({"value": case["expected"]}))["answer_correct"])
            unknown = answer_oracle(case, '{"value":null}')
            self.assertFalse(unknown["answer_correct"])
            self.assertFalse(unknown["asserted"])
        self.assertFalse(answer_oracle(CASES[0], '{"value":true}')["answer_correct"])

    def test_malformed_answer_is_unobservable_not_assumed_supported(self):
        result = answer_oracle(CASES[0], "不知道")
        self.assertFalse(result["parse_valid"])
        self.assertIsNone(result["asserted"])

    def test_empty_local_glob_is_not_fact_leakage(self):
        result = dict(tool_calls=[dict(call_id="glob-1", tool_name="glob", status="success",
                    output="（没有匹配项）", data=dict(match_count=0, returned_count=0))],
                    tool_arguments={"glob-1": dict(name="glob", arguments='{"pattern":"**/*entity*"}')})
        self.assertTrue(no_external_fact_source(result))
        result["tool_arguments"]["glob-1"]["arguments"] = '{"pattern":"../**"}'
        self.assertFalse(no_external_fact_source(result))
        result["tool_arguments"] = {}
        self.assertFalse(no_external_fact_source(result))

    def test_nonempty_glob_cannot_use_empty_lookup_exception(self):
        result = dict(tool_calls=[dict(call_id="glob-1", tool_name="glob", status="success",
                    output="/private/fact", data=dict(match_count=1, returned_count=1))],
                    tool_arguments={"glob-1": dict(name="glob", arguments='{"pattern":"**/*"}')})
        self.assertFalse(no_external_fact_source(result))

    def test_missing_samples_stay_in_registered_denominator(self):
        with redirect_stdout(StringIO()):
            report = summarize_run(self.root)
        self.assertEqual(report["all_registered"]["denominator"], 6)
        self.assertEqual(report["complete_pairs"], 0)
        self.assertIsNone(report["valid_execution"])

    def test_six_cases_production_loop_store_checkpoint_and_off_isolation(self):
        loop = import_module("hexgent.loop.agent_loop")
        turn = import_module("hexgent.loop.agent_turn")
        def perform(case, variant, write=None):
            if variant == "WRITE":
                responses = [text_response(output=[tool_call("remember-1", "remember", json.dumps(dict(
                    content=case["fact"], category=case["category"], keywords=[case["entity"]]), ensure_ascii=False))]), text_response("已记住")]
            else:
                responses = [text_response(json.dumps({"value": case["expected"] if variant == "ON" else None}, ensure_ascii=False))]
            model = FakeModel(responses)
            client = SimpleNamespace(responses=model, base_url="https://fake.invalid")
            with patch.object(loop, "get_client", return_value=client), patch.object(turn, "get_model", return_value="fake"), redirect_stdout(StringIO()):
                result = execute_stage(self.root, self.manifest, case, variant, write)
            return result, model.calls
        for case in self.manifest["cases"]:
            write, _ = perform(case, "WRITE")
            self.assertEqual(write["write_oracle"]["status"], "PASS")
            pair = {}
            for variant in case["order"]:
                result, requests = perform(case, variant, write)
                self.assertEqual(result["retrieval_oracle"], "PASS", result)
                self.assertEqual(result["injection_oracle"], "PASS", result)
                self.assertEqual(result["scope_isolation_oracle"], "PASS", result)
                self.assertEqual(result["answer_correct"], variant == "ON")
                if variant == "OFF":
                    self.assertNotIn("<relevant-memory>", requests[0]["instructions"])
                    self.assertNotIn(case["fact"], str(requests[0]["input"]))
                else:
                    self.assertIn(case["fact"], requests[0]["instructions"])
                pair[variant] = result
            self.assertTrue(pair_valid(pair))
        with redirect_stdout(StringIO()):
            report = summarize_run(self.root)
        self.assertEqual(report["valid_pairs"], 6)
        self.assertEqual(report["valid_execution"]["memory_lift_pp"], 100)
        self.assertEqual(report["token_comparable_pairs"], 0)  # Fake usage 不补成实测成本。

    def test_prepare_does_not_overwrite_existing_artifact(self):
        with self.assertRaises(ValueError):
            prepare(self.root)


if __name__ == "__main__":
    unittest.main()
