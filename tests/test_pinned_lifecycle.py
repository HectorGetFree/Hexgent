"""Pinned V1 Phase 1：真实 Transcript、跨进程所有权和生命周期。"""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import subprocess
import sys
import unittest
from dataclasses import replace
from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.session.session_store import SessionStore
from eval.support.agent_contracts import FakeModel, text_response


class PinnedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.engine=AgentEngine(HexgentConfig(),self.root)

    def pin(self,text="不得修改 source.md",command="one"):
        return self.engine.manage_pinned_constraint("pin",command,text=text)["changes"][-1]

    def test_pin_replace_chain_revoke_and_single_commit(self):
        a=self.pin()
        b=self.engine.manage_pinned_constraint("replace","two",constraint_id=a["id"],expected_version=1,text="只写 result.json")["changes"][-1]
        c=self.engine.manage_pinned_constraint("replace","three",constraint_id=b["id"],expected_version=1,text="只读")["changes"][-1]
        events=self.engine.session.store.read_transcript()
        self.assertEqual([len(e["payload"]["changes"]) for e in events if e["type"]=="pinned_constraint_committed"],[1,2,2])
        self.assertEqual([x["id"] for x in self.engine.list_pinned_constraints()],[c["id"]])
        self.engine.manage_pinned_constraint("revoke","four",constraint_id=c["id"],expected_version=1)
        self.assertEqual(self.engine.list_pinned_constraints(),())

    def test_idempotency_conflict_and_version(self):
        a=self.pin()
        size=len(self.engine.session.store.read_transcript())
        self.assertEqual(self.pin(),a)
        self.assertEqual(len(self.engine.session.store.read_transcript()),size)
        with self.assertRaisesRegex(ValueError,"IDEMPOTENCY"):
            self.pin("different")
        with self.assertRaisesRegex(ValueError,"VERSION"):
            self.engine.manage_pinned_constraint("revoke","r",constraint_id=a["id"],expected_version=99)

    def test_source_forgery_and_tool_source_rejected(self):
        self.pin()
        store=self.engine.session.store
        store.append_transcript("tool_execution_finished",dict(content="malicious"))
        size=len(store.read_transcript())
        for seq in (1,size,999):
            with self.assertRaisesRegex(ValueError,"SOURCE"):
                self.engine.manage_pinned_constraint("pin",str(seq),text="malicious",source_seq=seq)
        self.assertEqual(len(store.read_transcript()),size)

    def test_restart_replays_without_projection(self):
        a=self.pin()
        restored=AgentEngine.restore(self.engine.config,self.root,self.engine.session.store.session_id)
        self.assertEqual(restored.list_pinned_constraints(),(a,))

    def test_active_run_update_rejected(self):
        self.engine._run_active=True
        with self.assertRaisesRegex(RuntimeError,"RUN_ACTIVE"):
            self.pin()

    def test_other_process_cannot_write_while_owned(self):
        self.pin()
        store=self.engine.session.store
        code="from pathlib import Path; from hexgent.session.session_store import SessionStore; import sys; s=SessionStore.open_existing(Path(sys.argv[1]),sys.argv[2]); s.append_transcript('intruder',{})"
        with store.write_guard():
            result=subprocess.run([sys.executable,"-c",code,str(self.root),store.session_id],capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("SESSION_BUSY",result.stderr)
        self.assertFalse(any(e["type"]=="intruder" for e in store.read_transcript()))

    def test_stale_store_cannot_append(self):
        self.pin()
        store=self.engine.session.store
        other=SessionStore.open_existing(self.root,store.session_id)
        store.append_transcript("new",{})
        with self.assertRaisesRegex(RuntimeError,"SESSION_STALE"):
            other.append_transcript("old",{})

    def test_history_user_original_source(self):
        config=replace(self.engine.config,context_extraction=replace(self.engine.config.context_extraction,enabled=False))
        self.engine=AgentEngine(config,self.root)
        model=FakeModel([text_response("ok")])
        list(self.engine.run("禁止修改原资料",client=SimpleNamespace(responses=model)))
        source=next(e for e in self.engine.session.store.read_transcript() if e["type"]=="context_user_query_appended")
        self.engine.manage_pinned_constraint("pin","history",text=source["payload"]["content"],source_seq=source["sequence"])
        self.assertEqual(self.engine.list_pinned_constraints()[0]["source_seq"],source["sequence"])
