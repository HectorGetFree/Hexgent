"""长期 Memory 生命周期：真实本地持久化和独立请求 Oracle，不调用真实模型。"""

import json
import multiprocessing
import asyncio
import os
import shutil
from io import StringIO
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context.context_builder import build_context_details
from hexgent.memory import MemoryStore, MemoryRetriever
from hexgent.loop.call_model import call_model
from hexgent.run_control import RunControl
from hexgent.tool.tool_execution import execute_tool_call
from eval.support.agent_contracts import FakeModel, text_response, tool_call


def mutate(root, identity, action, barrier, queue):
    store = MemoryStore(Path(root))
    barrier.wait()
    try:
        if action == "delete":
            store.delete(identity)
        else:
            store.supersede(identity, "Zeta port 49273", "project", keywords=["Zeta"])
        queue.put(action + ":OK")
    except ValueError as error:
        queue.put(action + ":" + str(error))


class MemoryLifecycleTest(TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = MemoryStore(self.root)
        self.item = self.store.upsert("Zeta port 48271", "project", keywords=["Zeta"], source="explicit").item

    def tearDown(self):
        """独立验收运行保存原始合成 Store/Transcript，普通回归不产生工件。"""
        output = os.environ.get("M2_EVIDENCE_DIR")
        if output:
            target = Path(output) / self._testMethodName
            shutil.copytree(self.root, target)
            (target / "provider-requests.json").write_text(json.dumps(
                getattr(self, "provider_requests", []), ensure_ascii=False, default=str,
            ), encoding="utf-8")

    def recalled(self):
        return MemoryRetriever().retrieve("Zeta", self.store.items)

    def test_single_and_chained_supersede(self):
        second = self.store.supersede(self.item.memory_id, "Zeta port 49273", "project").item
        self.assertEqual([m.memory_id for m in self.store.items], [second.memory_id])
        self.assertEqual(self.store.supersede(self.item.memory_id, second.content, "project").item.memory_id, second.memory_id)
        third = self.store.supersede(second.memory_id, "Zeta port 50191", "project").item
        self.assertEqual([m.memory_id for m in MemoryStore(self.root).items], [third.memory_id])
        authority = json.loads(self.store.lifecycle_path.read_text())
        old = next(i for i in authority["items"] if i["memory_id"] == self.item.memory_id)
        self.assertEqual(old["status"], "SUPERSEDED")
        self.assertEqual(old["superseded_by"], second.memory_id)

    def test_self_cycle_and_conflict_rejected(self):
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.store.supersede(self.item.memory_id, self.item.content, "project")
        second = self.store.supersede(self.item.memory_id, "Zeta port 49273", "project").item
        with self.assertRaisesRegex(ValueError, "REVOKED"):
            self.store.supersede(second.memory_id, self.item.content, "project")
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            self.store.supersede(self.item.memory_id, "Zeta port 55555", "project")

    def test_invalidate_idempotent_and_no_resurrection(self):
        self.store.invalidate(self.item.memory_id)
        revision = json.loads(self.store.lifecycle_path.read_text())["revision"]
        self.store.invalidate(self.item.memory_id)
        self.assertEqual(json.loads(self.store.lifecycle_path.read_text())["revision"], revision)
        self.assertEqual(MemoryStore(self.root).items, ())
        self.assertTrue((self.store.items_directory / (self.item.memory_id + ".md")).exists())
        with self.assertRaisesRegex(ValueError, "REVOKED"):
            self.store.upsert(self.item.content, "project", source="explicit")

    def test_delete_body_and_tombstone_survive_restart(self):
        self.store.delete(self.item.memory_id)
        self.store.delete(self.item.memory_id)
        self.assertFalse((self.store.items_directory / (self.item.memory_id + ".md")).exists())
        self.assertNotIn(self.item.content, self.store.lifecycle_path.read_text())
        restarted = MemoryStore(self.root)
        self.assertEqual(restarted.items, ())
        with self.assertRaisesRegex(ValueError, "REVOKED"):
            restarted.upsert(self.item.content, "project", source="explicit")

    def test_missing_identity_and_other_scope_cannot_be_modified(self):
        other = MemoryStore(self.root / "other")
        for action in (other.invalidate, other.delete):
            with self.assertRaisesRegex(ValueError, "NOT_FOUND"):
                action(self.item.memory_id)
        self.assertEqual(self.recalled()[0].content, self.item.content)

    def test_existing_store_refreshes_after_external_api_mutation(self):
        stale = MemoryStore(self.root)
        self.assertEqual(len(stale.items), 1)
        self.store.invalidate(self.item.memory_id)
        self.assertEqual(stale.items, ())

    def test_builder_rejects_previously_recalled_memory(self):
        candidates = self.recalled()
        engine = AgentEngine(HexgentConfig(), self.root)
        self.store.invalidate(self.item.memory_id)
        built = build_context_details(engine.tool_context, relevant_memory=candidates)
        self.assertNotIn("<relevant-memory>", built.instructions)
        self.assertEqual(built.memory_receipt, ())

    def test_actual_request_rechecks_after_context_was_built(self):
        engine = AgentEngine(HexgentConfig(), self.root)
        built = build_context_details(engine.tool_context, relevant_memory=self.recalled())
        self.store.delete(self.item.memory_id)
        model = FakeModel([text_response("unknown")])
        list(call_model(SimpleNamespace(responses=model), memory_store=engine.memory_store, instructions=built.instructions, model="fake", input=[]))
        self.assertNotIn(self.item.content, model.calls[0]["instructions"])
        self.provider_requests = model.calls

    def test_formal_checkpoint_restore_continue_revalidates(self):
        engine = AgentEngine(HexgentConfig(), self.root)
        control = RunControl()
        model = FakeModel([text_response("first")])
        def pause_before_create(**kwargs):
            control.pause()
            return original(**kwargs)
        original = model.create
        with patch.object(model, "create", side_effect=pause_before_create):
            list(engine.run("Zeta", client=SimpleNamespace(responses=model), run_control=control))
        self.assertTrue(engine.has_continuable_run)
        identity = engine.session.store.session_id
        self.store.invalidate(self.item.memory_id)
        restored = AgentEngine.restore(HexgentConfig(), self.root, identity)
        next_model = FakeModel([text_response("unknown")])
        events = list(restored.run(RunRequest.continue_run(), client=SimpleNamespace(responses=next_model)))
        self.assertEqual(events[-1].result.status, "completed")
        self.assertNotIn(self.item.content, next_model.calls[0]["instructions"])
        self.provider_requests = next_model.calls

    def test_supersede_delete_cross_process_race(self):
        ctx = multiprocessing.get_context("spawn")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=mutate, args=(str(self.root), self.item.memory_id, action, barrier, queue))
                     for action in ("supersede", "delete")]
        for process in processes:
            process.start()
        for process in processes:
            process.join(15)
            self.assertEqual(process.exitcode, 0)
        outcomes = [queue.get(timeout=2) for _ in processes]
        self.assertIn("delete:OK", outcomes)
        self.assertNotIn(self.item.memory_id, [m.memory_id for m in MemoryStore(self.root).items])
        self.assertIn("supersede:OK" if len(self.store.items) else "supersede:MEMORY_NOT_FOUND", outcomes)

    def test_lifecycle_tools_production_pipeline_and_permission(self):
        engine = AgentEngine(HexgentConfig(), self.root)
        args = json.dumps({"memory_id": self.item.memory_id})
        model = FakeModel([text_response(output=[tool_call("revoke", "invalidate_memory", args)]), text_response("done")])
        list(engine.run("明确撤销", client=SimpleNamespace(responses=model), user_confirmation=lambda *_: "deny"))
        self.assertEqual(len(self.store.items), 1)
        model = FakeModel([text_response(output=[tool_call("revoke2", "invalidate_memory", args)]), text_response("done")])
        list(engine.run("明确撤销", client=SimpleNamespace(responses=model), user_confirmation=lambda *_: "allow"))
        self.assertEqual(self.store.items, ())

    def test_delete_cleanup_failure_stays_revoked(self):
        with patch.object(Path, "unlink", side_effect=OSError("cleanup failed")):
            with self.assertRaises(OSError):
                self.store.delete(self.item.memory_id)
        self.assertEqual(MemoryStore(self.root).items, ())
        self.store.delete(self.item.memory_id)
        self.assertFalse((self.store.items_directory / (self.item.memory_id + ".md")).exists())

    def test_remember_cache_cannot_bypass_external_revocation(self):
        engine = AgentEngine(HexgentConfig(), self.root)
        args = json.dumps({"content": self.item.content, "category": "project", "keywords": []})
        first = execute_tool_call("remember", args, context=engine.tool_context, call_id="first")
        self.assertEqual(first.status, "success")
        self.assertEqual(execute_tool_call("remember", args, context=engine.tool_context, call_id="repeat").status, "deduplicated")
        self.store.invalidate(self.item.memory_id)
        result = execute_tool_call("remember", args, context=engine.tool_context, call_id="revoked")
        self.assertEqual(result.failure.code, "MEMORY_REVOKED")
        self.assertEqual(self.store.items, ())

    def test_uncommitted_new_body_is_not_retrievable(self):
        with patch.object(self.store, "_commit_authority", side_effect=OSError("crash before commit")):
            with self.assertRaises(OSError):
                self.store.supersede(self.item.memory_id, "Zeta port 49273", "project")
        self.assertEqual([m.content for m in MemoryStore(self.root).items], [self.item.content])

    def test_post_commit_index_failure_does_not_restore_old_fact(self):
        with patch.object(self.store, "_write_index", side_effect=OSError("index failed")):
            with self.assertRaises(OSError):
                self.store.supersede(self.item.memory_id, "Zeta port 49273", "project")
        self.assertEqual([m.content for m in MemoryStore(self.root).items], ["Zeta port 49273"])

    def test_model_retry_rechecks_revocation(self):
        engine = AgentEngine(HexgentConfig(), self.root)
        built = build_context_details(engine.tool_context, relevant_memory=self.recalled())
        model = FakeModel([text_response("unknown")])
        original = model.create
        def create(**kwargs):
            if not model.calls:
                model.calls.append(kwargs)
                def broken_stream():
                    self.store.invalidate(self.item.memory_id)
                    raise RuntimeError("peer closed connection incomplete chunked read")
                    yield
                return broken_stream()
            return original(**kwargs)
        with patch.object(model, "create", side_effect=create):
            list(call_model(SimpleNamespace(responses=model), memory_store=engine.memory_store,
                            instructions=built.instructions, model="fake", input=[]))
        self.assertIn(self.item.content, model.calls[0]["instructions"])
        self.assertNotIn(self.item.content, model.calls[1]["instructions"])
        self.provider_requests = model.calls

    def test_cli_and_service_use_the_same_lifecycle_tools(self):
        from hexgent.adapters.web.run_manager import RunManager
        from importlib import import_module
        cli_module = import_module("hexgent.adapters.cli.main")
        for adapter in ("CLI", "Service"):
            root = self.root / adapter
            item = MemoryStore(root).upsert("Zeta port 48271", "project", source="explicit").item
            model = FakeModel([text_response(output=[tool_call("delete", "delete_memory", json.dumps({"memory_id": item.memory_id}))]), text_response("done")])
            client = SimpleNamespace(responses=model)
            with patch("hexgent.loop.agent_loop.get_client", return_value=client):
                if adapter == "CLI":
                    terminal = SimpleNamespace(read_message=iter(["明确删除记忆", "exit"]).__next__,
                                               request_confirmation=lambda *_: "allow",
                                               capture_run_interrupt=lambda *_: nullcontext())
                    with patch.object(cli_module, "find_workspace_root", return_value=root), patch.object(cli_module, "TerminalIO", return_value=terminal), patch("sys.stdout", new=StringIO()):
                        cli_module.main()
                else:
                    async def service():
                        manager = RunManager(HexgentConfig(), root)
                        try:
                            session = await manager.create_session("auto_approve")
                            response = await manager.start_run(session.session_id, "明确删除记忆")
                            await manager._runs[response.run_id].task
                            self.assertEqual(manager._runs[response.run_id].status, "completed")
                        finally:
                            await manager.shutdown()
                    asyncio.run(service())
            self.assertEqual(MemoryStore(root).items, ())
            self.assertNotIn("Zeta port 48271", MemoryStore(root).lifecycle_path.read_text())
