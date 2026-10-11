"""Phase 1 局部兼容修复：首次加载、陈旧写入和跨进程序号。"""
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.events import ToolFinishedEvent
from hexgent.run_control import RunControl
from hexgent.session.session_store import SessionStore
from eval.support.agent_contracts import FakeModel, text_response, tool_call


class SessionStoreCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = SessionStore(self.root, session_id="compatibility")
        self.store.start_session("用户原始任务")

    def test_r1_first_load_restores_next_sequence_and_preserves_header(self):
        before = self.store.transcript_path.read_bytes()
        fresh = SessionStore(self.root, session_id=self.store.session_id)
        self.assertEqual(fresh.next_sequence, 2)
        fresh.start_session("不得覆盖原任务")
        self.assertEqual(fresh.transcript_path.read_bytes(), before)
        fresh.append_transcript("legal", {})
        records = fresh.read_transcript()
        self.assertEqual([event["sequence"] for event in records], [1, 2])
        self.assertEqual(records[-1]["type"], "legal")

    def test_r2_loaded_writer_stays_stale_after_legal_takeover(self):
        # A 已释放所有权，B 加载并合法提交，再由 A 重新获取锁尝试旧状态。
        a = SessionStore(self.root, session_id=self.store.session_id)
        b = SessionStore(self.root, session_id=self.store.session_id)
        b.append_transcript("writer_b", {})
        before = b.transcript_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "SESSION_STALE"):
            a.append_transcript("writer_a", {})
        self.assertEqual(a.next_sequence, 2)
        self.assertEqual(a.transcript_path.read_bytes(), before)

    def test_r3_two_processes_contend_without_duplicate_sequence(self):
        code = '''
from pathlib import Path
import sys
from hexgent.session.session_store import SessionStore
s = SessionStore(Path(sys.argv[1]), session_id=sys.argv[2])
print("READY", flush=True)
sys.stdin.readline()
try:
    with s.write_guard():
        s.append_transcript("child_committed", {})
        print("COMMITTED", flush=True)
        sys.stdin.readline()
except RuntimeError as error:
    print(str(error), flush=True)
'''
        children = []
        try:
            for _ in range(2):
                child = subprocess.Popen([sys.executable, "-c", code, str(self.root), self.store.session_id],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                children.append(child)
                self.assertEqual(child.stdout.readline().strip(), "READY")
            for child in children:
                child.stdin.write("GO\n")
                child.stdin.flush()
            outcomes = [child.stdout.readline().strip() for child in children]
            self.assertEqual(outcomes.count("COMMITTED"), 1)
            self.assertEqual(sum("SESSION_BUSY" in result for result in outcomes), 1)
            for child, result in zip(children, outcomes):
                output, error = child.communicate("RELEASE\n" if result == "COMMITTED" else None, timeout=10)
                self.assertEqual(child.returncode, 0, error)
            fresh = SessionStore(self.root, session_id=self.store.session_id)
            fresh.append_transcript("after_takeover", {})
            self.assertEqual([event["sequence"] for event in fresh.read_transcript()], [1, 2, 3])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=10)

    def test_r4_corruption_or_gap_rejected_without_overwrite(self):
        valid = self.store.transcript_path.read_text()
        malformed = [valid + "not-json\n", valid + json.dumps(dict(schema_version=2, sequence=3, type="gap", payload={})) + "\n"]
        for content in malformed:
            with self.subTest(content=content):
                self.store.transcript_path.write_text(content)
                with self.assertRaises(ValueError):
                    SessionStore(self.root, session_id=self.store.session_id)
                self.assertEqual(self.store.transcript_path.read_text(), content)

    def test_r6_lifecycle_then_formal_restore_continue(self):
        base = HexgentConfig()
        cfg = replace(base, context_extraction=replace(base.context_extraction, enabled=False),
                      memory_retrieval=replace(base.memory_retrieval, enabled=False))
        engine = AgentEngine(cfg, self.root)
        a = engine.manage_pinned_constraint("pin", "a", text="只读原资料")["changes"][-1]
        control = RunControl()
        model = FakeModel([text_response(output=[tool_call("pause", "glob", '{"pattern":"*.absent"}')])])
        for event in engine.run("检查资料后暂停", client=SimpleNamespace(responses=model), run_control=control):
            if isinstance(event, ToolFinishedEvent):
                control.pause()
        self.assertTrue(engine.has_continuable_run)
        b = engine.manage_pinned_constraint("replace", "b", constraint_id=a["id"], expected_version=1,
                                          text="只写结果文件")["changes"][-1]
        session_id = engine.session.store.session_id
        engine = AgentEngine.restore(cfg, self.root, session_id)
        self.assertEqual([row["id"] for row in engine.list_pinned_constraints()], [b["id"]])
        engine.manage_pinned_constraint("revoke", "r", constraint_id=b["id"], expected_version=1)
        engine = AgentEngine.restore(cfg, self.root, session_id)
        queries_before = len(engine.context_manager.user_queries)
        model = FakeModel([text_response("正式续跑完成")])
        events = list(engine.run(RunRequest.continue_run(), client=SimpleNamespace(responses=model)))
        self.assertEqual(events[-1].result.status, "completed")
        self.assertEqual(len(engine.context_manager.user_queries), queries_before)
        self.assertEqual(engine.session.store.session_id, session_id)
        self.assertEqual(engine.list_pinned_constraints(), ())
        self.assertEqual(len(model.calls), 1)
