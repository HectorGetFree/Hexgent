"""独立 Phase 3 验收：只使用 Production 管理 API，首个失败后停止。"""
import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import unittest

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.session.pinned_constraints import replay_constraints


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def hashes(root):
    return {p.relative_to(root).as_posix(): sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "hexgent").rglob("*.py"))}


class ProductionAcceptance(unittest.TestCase):
    output = None

    def setUp(self):
        self.case = self.output / self._testMethodName
        self.case.mkdir()
        self.engine = AgentEngine(HexgentConfig(), self.case / "workspace")
        self.actions = []

    def command(self, action, command_id, **arguments):
        result = self.engine.manage_pinned_constraint(action, command_id, **arguments)
        self.actions.append(dict(action=action, command_id=command_id, arguments=arguments, result=result))
        return result

    def tearDown(self):
        store = self.engine.session.store
        records = store.read_transcript() if store.session_id else []
        state, commands = replay_constraints(records)
        save(self.case / "evidence.json", dict(actions=self.actions, transcript=records,
             projection={key: asdict(value) for key, value in state.items()},
             command_projection=commands, session_id=store.session_id,
             provider_dispatch_count=0, evidence_type="PRODUCTION_DETERMINISTIC_API"))

    def test_01_lifecycle_single_event_chain_and_replay(self):
        a = self.command("pin", "pin-A", text="禁止修改资料")["changes"][-1]
        snapshot = self.engine.session.store.read_transcript()
        self.assertEqual(self.command("pin", "pin-A", text="禁止修改资料")["changes"][-1], a)
        self.assertEqual(self.engine.session.store.read_transcript(), snapshot)
        b = self.command("replace", "replace-B", constraint_id=a["id"], expected_version=1, text="只写结果")["changes"][-1]
        changes = self.engine.session.store.read_transcript()[-1]["payload"]["changes"]
        self.assertEqual([row["status"] for row in changes], ["SUPERSEDED", "ACTIVE"])
        self.assertEqual(changes[0]["superseded_by"], b["id"])
        c = self.command("replace", "replace-C", constraint_id=b["id"], expected_version=1, text="恢复只读")["changes"][-1]
        before = self.engine.session.store.read_transcript()
        with self.assertRaisesRegex(ValueError, "VERSION_CONFLICT"):
            self.command("revoke", "wrong-version", constraint_id=c["id"], expected_version=99)
        self.assertEqual(self.engine.session.store.read_transcript(), before)
        self.command("revoke", "revoke-C", constraint_id=c["id"], expected_version=1)
        state_before, commands_before = replay_constraints(self.engine.session.store.read_transcript())
        self.engine = AgentEngine.restore(self.engine.config, self.case / "workspace", self.engine.session.store.session_id)
        state_after, commands_after = replay_constraints(self.engine.session.store.read_transcript())
        self.assertEqual(state_before, state_after)
        self.assertEqual(commands_before, commands_after)
        self.assertEqual(self.engine.list_pinned_constraints(), ())

    def test_02_noop_revoke_command_identity_must_be_durable(self):
        a = self.command("pin", "pin", text="禁止修改资料")["changes"][-1]
        self.command("revoke", "first-revoke", constraint_id=a["id"], expected_version=1)
        noop = self.command("revoke", "noop-command", constraint_id=a["id"], expected_version=2)
        self.assertEqual(noop["changes"], [])
        before = self.engine.session.store.read_transcript()
        # 同 command_id 改变输入必须冲突，不能因为先前无状态变更就忘记命令身份。
        with self.assertRaisesRegex(ValueError, "IDEMPOTENCY_CONFLICT"):
            self.command("pin", "noop-command", text="另一条约束")
        self.assertEqual(self.engine.session.store.read_transcript(), before)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    before = hashes(repo)
    save(output / "manifest.json", dict(production_hashes=before,
        implementation_files=[path for path in before if any(word in path for word in
            ("pinned", "session", "context", "agent_turn", "call_model", "adapters/cli"))],
        tests={str(p.relative_to(repo)): sha256(p.read_bytes()).hexdigest() for p in
            sorted((repo / "tests").glob("test_*pinned*.py")) + [repo / "tests/test_session_store_compatibility.py"]},
        authority="完整 Transcript；Projection 可重放，Checkpoint 不保存 Pinned 正文",
        request_path="AgentEngine.run → agent_turn.current_context/prepare_current_request → build_context_details → call_model → client.responses.create",
        policy="首个 Production 缺陷停止；不修改 Production；不调用付费模型"))
    ProductionAcceptance.output = output
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProductionAcceptance)
    result = unittest.TextTestRunner(verbosity=2, failfast=True).run(suite)
    after = hashes(repo)
    save(output / "result.json", dict(status="STOPPED_ON_PRODUCTION_DEFECT" if not result.wasSuccessful() else "PASS",
        tests_run=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
        failure_details=[dict(test=test.id(), traceback=trace) for test, trace in result.failures + result.errors],
        production_unchanged=before == after, production_hashes_after=after,
        not_tested=["完整来源与跨 Session 来源矩阵", "Phase3 独立请求/摘要矩阵", "Phase3 独立预算矩阵", "Phase3 并发故障矩阵", "Phase3 权限与兼容矩阵"],
        conclusion="NO_GO_FOR_REAL_MODEL_SMOKE" if not result.wasSuccessful() else "REVIEW_REQUIRED"))
    if not result.wasSuccessful():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
