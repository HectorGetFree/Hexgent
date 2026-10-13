"""独立 Production/Fake 验收；不调用网络，不使用旧冻结 Eval Wrapper。"""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context import ContextLimits, ContextManager
from hexgent.context.context_compaction import compact_context_if_needed, CompactionPublicationError
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from hexgent.context.key_fact_snapshot import validate_snapshot, snapshot_message
from hexgent.session import Session
from hexgent.session.checkpoint import _context_snapshot, CheckpointStore
from hexgent.session.session_store import SessionStore
from hexgent.events import CheckpointCommittedEvent
from eval.support.agent_contracts import text_response, tool_call


FACTS = ["交接任务 AUDIT-MAPLE-57", "参数与协议已完成", "检索审查已完成",
         "等待维护者确认两处权限契约", "确认后生成交接报告"]


def drain(stream):
    while True:
        try:
            next(stream)
        except StopIteration as stop:
            return stop.value


def raw_snapshot(payload):
    sources = payload["history_prefix"]
    previous = payload["previous_snapshot"]
    if previous:
        facts = deepcopy(previous["facts"])
        dispositions = [dict(old_fact_id=f["id"], action="CARRY_FORWARD", new_fact_id=f["id"], source_refs=[])
                        for f in facts]
    else:
        source = next((s["message_id"] for s in sources if "AUDIT-MAPLE-57" in json.dumps(s, ensure_ascii=False)), sources[0]["message_id"])
        facts = [dict(id=f"f{i}", content=f, source_refs=[source], status="active") for i, f in enumerate(FACTS)]
        dispositions = []
    return dict(as_of_message_id=payload["as_of_message_id"], facts=facts, dispositions=dispositions)


class Model:
    def __init__(self, extraction=None, summary="<summary>其余背景已压缩。</summary>", main=None):
        self.calls = []
        self.options = []
        self.extraction = extraction
        self.summary = summary
        self.main = list(main or [text_response("完成")])
        self.response_records = []
        self.responses = self

    def with_options(self, **options):
        self.options.append(options)
        return self

    def create(self, **request):
        self.calls.append(deepcopy(request))
        if request["instructions"] == EXTRACTION_INSTRUCTIONS:
            payload = json.loads(request["input"][0]["content"])
            value = self.extraction(payload) if callable(self.extraction) else self.extraction
            if isinstance(value, Exception):
                raise value
            value = raw_snapshot(payload) if value is None else value
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            response = text_response(text)
        elif "你负责压缩 Agent" in request["instructions"]:
            if isinstance(self.summary, Exception):
                raise self.summary
            response = text_response(self.summary)
        else:
            response = self.main.pop(0)
        self.response_records.append(dict(output_text=response.output_text,
            usage=vars(response.usage) if getattr(response, "usage", None) is not None else None))
        return iter([SimpleNamespace(type="response.completed", response=response)])


class RemediationTests(unittest.TestCase):
    evidence = []

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.limits = replace(ContextLimits(), proactive_threshold_tokens=20000, recent_context_tokens=100,
                              reactive_recent_context_tokens=100,
                              min_compaction_savings_tokens=1)
        self.manager = ContextManager()
        self.manager.append_user_query("处理交接资料")
        self.manager.append_messages([dict(role="assistant", content="；".join(FACTS) + "背景资料。"*5000)], completes_round=True)
        self.manager.append_user_query("继续")
        self.session = Session.persistent(self.root)
        self.session.initialize_runtime_session("remediation-fixture")
        for message in self.manager.active_messages:
            if hasattr(message, "sequence"):
                self.session.record_user_query(message)
            else:
                self.session.record_context_messages([message], completes_round=True, source="fixture_history")

    def compact(self, model=None, manager=None, commit=None):
        model = model or Model()
        manager = manager or self.manager
        result = drain(compact_context_if_needed(manager, model, "offline", "系统", [], self.limits,
                                                force=True, commit_boundary=commit,
                                                plan_reference={"goal": "只读参考，不得作为事实来源"}))
        self.evidence.append(dict(test=self._testMethodName, outcome=asdict(result), requests=model.calls,
                                  responses=model.response_records,
                                  context=_context_snapshot(manager), transcript=self.session.store.read_transcript(),
                                  evidence_type="DETERMINISTIC_PRODUCTION_FAKE_MODEL"))
        return result, model

    def test_five_facts_survive_summary_omission_and_checkpoint(self):
        outcome, model = self.compact(commit=self.session.record_compaction)
        self.assertEqual(outcome.status, "compacted")
        text = json.dumps(self.manager.model_messages(), ensure_ascii=False)
        self.assertTrue(all(f in text for f in FACTS))
        self.assertNotIn("AUDIT-MAPLE", self.manager.active_messages[0]["content"])
        restored = ContextManager.from_checkpoint(_context_snapshot(self.manager))
        self.assertEqual(restored.model_messages(), self.manager.model_messages())
        self.assertEqual(model.options, [{"timeout": 120.0, "max_retries": 0}, {"max_retries": 0}])

    def test_source_schema_and_empty_reject_without_mutation(self):
        for value in ("not-json", {}, lambda p: dict(as_of_message_id=p["as_of_message_id"], facts=[], dispositions=[]),
                      RuntimeError("Provider error"), TimeoutError("timeout")):
            before = deepcopy(self.manager.active_messages)
            self.manager.consecutive_compaction_failures = 0
            outcome, model = self.compact(Model(extraction=value))
            self.assertIn(outcome.status, {"failed", "blocked"})
            self.assertEqual(before, self.manager.active_messages)
            self.assertIsNone(self.manager.key_fact_snapshot)
            self.assertEqual(len(model.calls), 1)

    def test_out_of_scope_intent_reference_is_not_source(self):
        def invalid(payload):
            raw = raw_snapshot(payload)
            raw["facts"][0]["source_refs"] = ["latest-query"]
            return raw
        outcome, _ = self.compact(Model(extraction=invalid))
        self.assertIn("OUT_OF_SCOPE", outcome.message)

    def test_snapshot_budget_no_truncation(self):
        self.limits = replace(self.limits, key_fact_snapshot_tokens=1)
        outcome, model = self.compact()
        self.assertIn("SNAPSHOT_LIMIT_EXCEEDED", outcome.message)
        self.assertEqual(len(model.calls), 1)

    def test_summary_budget_and_savings_rejection(self):
        for summary in ("<summary>"+"背景"*10000+"</summary>", "invalid", RuntimeError("failed")):
            self.manager.consecutive_compaction_failures = 0
            before = deepcopy(self.manager.active_messages)
            outcome, model = self.compact(Model(summary=summary))
            self.assertEqual(outcome.status, "failed")
            self.assertEqual(before, self.manager.active_messages)
            self.assertEqual(len(model.calls), 3)
        self.limits = replace(self.limits, min_compaction_savings_tokens=10**9, min_compaction_savings_ratio=1.0)
        self.manager.consecutive_compaction_failures = 0
        outcome, _ = self.compact()
        self.assertIn("收益不足", outcome.message)

    def test_old_fact_dispositions_and_replacement(self):
        self.compact(commit=self.session.record_compaction)
        old = deepcopy(self.manager.key_fact_snapshot)
        self.manager.append_messages([dict(role="assistant", content="新证据：确认已完成；"+"新背景。"*5000)], completes_round=True)
        self.session.record_context_messages(self.manager.active_messages[-1:], completes_round=True, source="new_evidence")
        self.manager.append_user_query("继续生成报告")
        self.session.record_user_query(self.manager.user_queries[-1])
        def update(payload):
            raw = raw_snapshot(payload)
            source = payload["history_prefix"][-1]["message_id"]
            raw["facts"][0].update(content="已确认任务", source_refs=[source])
            raw["dispositions"][0].update(action="UPDATED", source_refs=[source])
            raw["facts"][1].update(status="uncertain", content="参数状态不确定", source_refs=[source])
            raw["dispositions"][1].update(action="UNCERTAIN", source_refs=[source])
            raw["facts"] = [f for f in raw["facts"] if f["id"] != "f2"]
            raw["dispositions"][2].update(action="SUPERSEDED", new_fact_id=None, source_refs=[source])
            return raw
        outcome, _ = self.compact(Model(extraction=update), commit=self.session.record_compaction)
        self.assertEqual(outcome.status, "compacted")
        self.assertEqual(len(self.manager.key_fact_snapshot["facts"]), 4)
        self.assertNotEqual(old, self.manager.key_fact_snapshot)
        self.assertEqual(len([m for m in self.manager.model_messages() if "<key-fact-snapshot>" in str(m)]), 1)

    def test_unhandled_old_fact_blocks(self):
        self.compact()
        self.manager.append_messages([dict(role="assistant", content="新背景。"*5000)], completes_round=True)
        before = deepcopy(self.manager.key_fact_snapshot)
        def missing(payload):
            raw = raw_snapshot(payload)
            raw["dispositions"].pop()
            return raw
        outcome, _ = self.compact(Model(extraction=missing))
        self.assertIn("OLD_FACT_UNHANDLED", outcome.message)
        self.assertEqual(before, self.manager.key_fact_snapshot)

    def test_temporary_write_failure_leaves_authority_unchanged(self):
        before = self.session.store.transcript_path.read_bytes()
        state = deepcopy(self.manager.active_messages)
        writer = SessionStore._write_compaction_temporary
        def partial(path, data):
            writer(path, data[:len(data)//2])
            raise OSError("partial write failed")
        with patch.object(SessionStore, "_write_compaction_temporary", staticmethod(partial)):
            outcome, _ = self.compact(commit=self.session.record_compaction)
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(before, self.session.store.transcript_path.read_bytes())
        self.assertEqual(state, self.manager.active_messages)

    def test_replace_failure_leaves_authority_unchanged(self):
        before = self.session.store.transcript_path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("replace failed")):
            outcome, _ = self.compact(commit=self.session.record_compaction)
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(before, self.session.store.transcript_path.read_bytes())
        self.assertIsNone(self.manager.key_fact_snapshot)
        self.assertEqual(list(self.session.store.session_dir.glob(".*.tmp")), [])

    def test_publish_failure_is_committed_and_restore_replays(self):
        original = ContextManager.restore_compaction
        with patch.object(ContextManager, "restore_compaction", side_effect=RuntimeError("projection")):
            with self.assertRaises(CompactionPublicationError):
                self.compact(commit=self.session.record_compaction)
        self.assertIsNone(self.manager.key_fact_snapshot)
        events = self.session.store.read_transcript()
        commit = events[-1]
        self.assertEqual(commit["type"], "context_compaction_applied")
        original(self.manager, commit["payload"])
        self.assertIsNotNone(self.manager.key_fact_snapshot)
        restored = AgentEngine.restore(HexgentConfig(), self.root, self.session.store.session_id)
        self.assertEqual(restored.context_manager.model_messages(), self.manager.model_messages())

    def test_atomic_replace_then_ordinary_append_and_sequence(self):
        self.session.record_runtime_event("ordinary_before", {})
        outcome, _ = self.compact(commit=self.session.record_compaction)
        self.session.record_runtime_event("ordinary_after", {})
        events = self.session.store.read_transcript()
        self.assertEqual([r["sequence"] for r in events], list(range(1, len(events)+1)))
        self.assertEqual([r["type"] for r in events[-3:]], ["ordinary_before", "context_compaction_applied", "ordinary_after"])
        self.assertGreater(outcome.boundary["publication_metrics"]["temporary_bytes"], 0)
        restored = AgentEngine.restore(HexgentConfig(), self.root, self.session.store.session_id)
        self.assertEqual(restored.context_manager.key_fact_snapshot, self.manager.key_fact_snapshot)

    def test_extraction_input_cut_and_intent_partition(self):
        _, model = self.compact()
        payload = json.loads(model.calls[0]["input"][0]["content"])
        self.assertEqual(payload["intent_reference"]["latest_query"], "继续")
        self.assertFalse(any(row["message"].get("content") == "继续" for row in payload["history_prefix"]))
        self.assertEqual(payload["as_of_message_id"], payload["history_prefix"][-1]["message_id"])

    def test_recent_updates_remain_after_as_of_snapshot(self):
        self.compact()
        self.manager.append_user_query("最新更正：权限已确认，不再等待")
        messages = self.manager.model_messages()
        self.assertIn("等待维护者", messages[0]["content"])
        self.assertIn("权限已确认", messages[-1]["content"])
        self.assertIn("仅截至标记切点", messages[0]["content"])

    def test_fake_main_request_sees_production_snapshot_and_no_side_effect(self):
        outcome, _ = self.compact(commit=self.session.record_compaction)
        config = HexgentConfig()
        config = replace(config, context=self.limits,
                         context_extraction=replace(config.context_extraction, enabled=False))
        engine = AgentEngine(config, self.root, context_manager=self.manager, session=self.session)
        model = Model()
        events = list(engine.run("请按当前事实继续", client=model))
        request = model.calls[-1]
        self.assertTrue(all(f in json.dumps(request["input"], ensure_ascii=False) for f in FACTS))
        self.assertEqual(events[-1].result.status, "completed")
        self.assertNotIn("key-fact-snapshot", request["instructions"])

    def tool_engine(self):
        root = self.root / "actual-tool"
        root.mkdir()
        (root / "source.txt").write_text("；".join(FACTS) + "\n" + "历史业务资料。"*9000, encoding="utf-8")
        config = HexgentConfig()
        config = replace(config,
            context=replace(config.context, proactive_threshold_tokens=16000,
                            recent_context_tokens=100, reactive_recent_context_tokens=100),
            context_extraction=replace(config.context_extraction, enabled=False),
            memory_retrieval=replace(config.memory_retrieval, enabled=False),
            context_retrieval=replace(config.context_retrieval, enabled=False),
            tool_result=replace(config.tool_result, max_inline_characters=200000, max_batch_inline_tokens=100000))
        model = Model(main=[text_response(output=[tool_call("actual-read", "read_file", '{"path":"source.txt"}')]),
                            text_response("完成")])
        return AgentEngine(config, root), model

    def pause_after_compaction(self, engine, model):
        stream = engine.run("读取 source.txt 并继续交接", client=model,
                            user_confirmation=lambda *args: "allow")
        for event in stream:
            if isinstance(event, CheckpointCommittedEvent) and event.reason == "context_compaction_completed":
                stream.close()
                return
        self.fail("实际文件工具路径没有触发 Compaction Checkpoint")

    def assert_formal_continue(self, engine):
        identity = engine.session.store.session_id
        restored = AgentEngine.restore(engine.config, engine.workdir, identity)
        self.assertTrue(restored.has_continuable_run)
        before = [r for r in restored.session.store.read_transcript() if r["type"] == "context_user_query_appended"]
        snapshot = deepcopy(restored.context_manager.key_fact_snapshot)
        model = Model()
        events = list(restored.run(RunRequest.continue_run(), client=model))
        self.assertEqual(events[-1].result.status, "completed")
        self.assertEqual(snapshot, restored.context_manager.key_fact_snapshot)
        self.assertFalse(any(r["instructions"] == EXTRACTION_INSTRUCTIONS for r in model.calls))
        self.assertTrue(all(f in json.dumps(model.calls[0]["input"], ensure_ascii=False) for f in FACTS))
        after = [r for r in restored.session.store.read_transcript() if r["type"] == "context_user_query_appended"]
        self.assertEqual(before, after)
        self.evidence.append(dict(test=self._testMethodName, requests=model.calls,
                                  transcript=restored.session.store.read_transcript(),
                                  formal_continue=True, original_session_id=identity,
                                  snapshot=_context_snapshot(restored.context_manager)))

    def test_real_file_tool_compaction_then_formal_restore_continue(self):
        engine, model = self.tool_engine()
        self.pause_after_compaction(engine, model)
        self.assertEqual(engine.context_manager.compaction_count, 1)
        self.assertEqual(len([r for r in model.calls if r["instructions"] == EXTRACTION_INSTRUCTIONS]), 1)
        self.assert_formal_continue(engine)

    def test_checkpoint_failure_preserves_commit_and_continue_from_event(self):
        engine, model = self.tool_engine()
        write = CheckpointStore.write
        def fail_only_compaction(store, **kw):
            if kw["reason"] == "context_compaction_completed":
                raise OSError("checkpoint write failure")
            return write(store, **kw)
        with patch.object(CheckpointStore, "write", fail_only_compaction):
            with self.assertRaisesRegex(RuntimeError, "COMPACTION_COMMITTED_RESTORE_REQUIRED"):
                list(engine.run("读取 source.txt 并继续交接", client=model, user_confirmation=lambda *args: "allow"))
        self.assertEqual(len([r for r in model.calls if r["instructions"] != EXTRACTION_INSTRUCTIONS and "你负责压缩 Agent" not in r["instructions"]]), 1)
        self.assert_formal_continue(engine)

    def test_projection_failure_formal_restore_continue_from_event(self):
        engine, model = self.tool_engine()
        with patch.object(ContextManager, "restore_compaction", side_effect=RuntimeError("projection")):
            with self.assertRaises(CompactionPublicationError):
                list(engine.run("读取 source.txt 并继续交接", client=model, user_confirmation=lambda *args: "allow"))
        self.assertIsNone(engine.context_manager.key_fact_snapshot)
        self.assert_formal_continue(engine)

    def test_atomic_commit_and_ordinary_event_thread_order(self):
        # 共用同一个 Store，普通追加必须等原子替换的锁，不可写入旧 inode。
        entered, release, attempting = Event(), Event(), Event()
        write = SessionStore._write_compaction_temporary
        def paused(path, data):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test synchronization timeout")
            write(path, data)
        def ordinary():
            attempting.set()
            self.session.record_runtime_event("concurrent_ordinary", {})
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(SessionStore, "_write_compaction_temporary", staticmethod(paused)):
            future = pool.submit(self.compact, commit=self.session.record_compaction)
            self.assertTrue(entered.wait(3))
            other = pool.submit(ordinary)
            self.assertTrue(attempting.wait(3))
            self.assertFalse(other.done())
            release.set()
            self.assertEqual(future.result()[0].status, "compacted")
            other.result()
        events = self.session.store.read_transcript()
        self.assertEqual([r["sequence"] for r in events], list(range(1, len(events)+1)))
        self.assertEqual([r["type"] for r in events[-2:]], ["context_compaction_applied", "concurrent_ordinary"])

    def test_old_checkpoint_without_new_fields_is_compatible(self):
        legacy = _context_snapshot(self.manager)
        for key in ("message_ids", "next_message_sequence", "key_fact_snapshot"):
            legacy.pop(key)
        restored = ContextManager.from_checkpoint(legacy)
        self.assertIsNone(restored.key_fact_snapshot)
        self.assertEqual(restored.model_messages(), self.manager.model_messages())

    def test_full_budget_with_snapshot_refuses_main(self):
        self.compact(commit=self.session.record_compaction)
        config = HexgentConfig()
        config = replace(config, context=replace(config.context, context_window_tokens=100,
                                                  reserved_output_tokens=1, safety_margin_tokens=1,
                                                  proactive_threshold_tokens=98,
                                                  recent_context_tokens=10, reactive_recent_context_tokens=10,
                                                  runtime_injection_tokens=100000),
                         context_extraction=replace(config.context_extraction, enabled=False))
        engine = AgentEngine(config, self.root, context_manager=self.manager, session=self.session)
        model = Model()
        events = list(engine.run("继续", client=model))
        self.assertEqual(events[-1].result.status, "failed")
        main = [r for r in model.calls if r["instructions"] != EXTRACTION_INSTRUCTIONS and "你负责压缩 Agent" not in r["instructions"]]
        self.assertEqual(main, [])

    def test_duplicate_disposition_and_carry_change_reject(self):
        self.compact()
        self.manager.append_messages([dict(role="assistant", content="背景。"*5000)], completes_round=True)
        for mutation in ("duplicate", "changed"):
            def invalid(payload):
                raw = raw_snapshot(payload)
                if mutation == "duplicate":
                    raw["dispositions"].append(raw["dispositions"][0])
                else:
                    raw["facts"][0]["content"] = "偷偷更改"
                return raw
            self.manager.consecutive_compaction_failures = 0
            outcome, _ = self.compact(Model(extraction=invalid))
            self.assertEqual(outcome.status, "failed")

    def test_repeated_compaction_does_not_accumulate_snapshot_blocks(self):
        for i in range(3):
            if i:
                self.manager.append_messages([dict(role="assistant", content="新增背景。"*5000)], completes_round=True)
            outcome, _ = self.compact()
            self.assertEqual(outcome.status, "compacted")
            self.assertEqual(len(self.manager.key_fact_snapshot["facts"]), 5)
            self.assertEqual(sum("<key-fact-snapshot>" in str(m) for m in self.manager.model_messages()), 1)

    def test_auxiliary_budget_guard_zero_dispatch(self):
        self.limits = replace(self.limits, auxiliary_context_window_tokens=100)
        outcome, model = self.compact()
        self.assertIn("EXTRACTOR_CONTEXT_BUDGET_EXCEEDED", outcome.message)
        self.assertEqual(model.calls, [])

    def test_failed_summary_cost_keeps_unknown_usage(self):
        outcome, _ = self.compact(Model(summary=TimeoutError("summary timeout")))
        self.assertEqual(len(outcome.cost_ledger["summary"]), 2)
        self.assertTrue(all(c["input_tokens"] is None for c in outcome.cost_ledger["summary"]))
        self.assertEqual(outcome.cost_ledger["amortized_benefit"], "AMORTIZED_BENEFIT_NOT_EVALUATED")

    def test_main_retry_keeps_snapshot_and_checks_actual_request(self):
        self.compact(commit=self.session.record_compaction)
        config = HexgentConfig()
        config = replace(config, context=self.limits,
                         context_extraction=replace(config.context_extraction, enabled=False))
        engine = AgentEngine(config, self.root, context_manager=self.manager, session=self.session)
        class EmptyStreamOnce(Model):
            def create(inner, **request):
                if not inner.calls:
                    inner.calls.append(deepcopy(request))
                    raise RuntimeError("peer closed connection: incomplete chunked read")
                return super().create(**request)
        model = EmptyStreamOnce()
        events = list(engine.run("继续", client=model))
        self.assertEqual(events[-1].result.status, "completed")
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(model.calls[0]["input"], model.calls[1]["input"])
        self.assertTrue(all(f in json.dumps(model.calls[1]["input"], ensure_ascii=False) for f in FACTS))
        self.evidence.append(dict(test=self._testMethodName, requests=model.calls,
                                  transcript=engine.session.store.read_transcript()))

    def test_legacy_checkpoint_then_new_snapshot_full_transcript_restore(self):
        boundary = self.manager.apply_compaction(2, "旧版摘要", before_tokens=1000,
                    after_tokens=100, summary_tokens=10, recent_tokens=10, status="compacted")
        self.session.record_compaction(boundary)
        legacy = _context_snapshot(self.manager)
        for key in ("message_ids", "next_message_sequence", "key_fact_snapshot", "source_ids_legacy"):
            legacy.pop(key)
        self.manager = ContextManager.from_checkpoint(legacy)
        new = dict(role="assistant", content="；".join(FACTS)+"背景。"*5000)
        self.manager.append_messages([new], completes_round=True)
        self.session.record_context_messages([new], completes_round=True, source="legacy_migration")
        self.manager.append_user_query("继续")
        self.session.record_user_query(self.manager.user_queries[-1])
        outcome, _ = self.compact(commit=self.session.record_compaction)
        self.assertEqual(outcome.status, "compacted")
        restored = AgentEngine.restore(HexgentConfig(), self.root, self.session.store.session_id)
        self.assertEqual(restored.context_manager.key_fact_snapshot, self.manager.key_fact_snapshot)

    def test_previous_summary_is_not_new_supersede_evidence(self):
        self.compact()
        self.manager.append_messages([dict(role="assistant", content="新资料。"*5000)], completes_round=True)
        def stale(payload):
            raw = raw_snapshot(payload)
            summary = next(s["message_id"] for s in payload["history_prefix"] if s["message_id"].startswith("summary-"))
            raw["facts"] = [f for f in raw["facts"] if f["id"] != "f0"]
            raw["dispositions"][0].update(action="SUPERSEDED", new_fact_id=None, source_refs=[summary])
            return raw
        outcome, _ = self.compact(Model(extraction=stale))
        self.assertIn("NEW_EVIDENCE_INVALID", outcome.message)
        self.assertEqual(len(self.manager.key_fact_snapshot["facts"]), 5)

    def test_latest_query_within_cut_is_reference_not_fact_source(self):
        self.manager.mark_round_complete()
        self.limits = replace(self.limits, recent_context_tokens=1, reactive_recent_context_tokens=1)
        forbidden = self.manager.message_ids[-1]
        def invalid(payload):
            self.assertNotIn(forbidden, [s["message_id"] for s in payload["history_prefix"]])
            raw = raw_snapshot(payload)
            raw["facts"][0]["source_refs"] = [forbidden]
            return raw
        outcome, _ = self.compact(Model(extraction=invalid))
        self.assertIn("SOURCE_OUT_OF_SCOPE", outcome.message)

    def test_previous_reference_query_can_become_later_history_evidence(self):
        self.manager = ContextManager()
        self.manager.append_user_query("核对端口")
        self.manager.append_messages([dict(role="assistant", content="旧端口48317。"+"背景。"*5000)], completes_round=True)
        self.manager.append_user_query("明确更正：当前端口59261")
        correction = self.manager.message_ids[-1]
        self.manager.append_messages([dict(role="assistant", content="已收到")], completes_round=True)
        self.limits = replace(self.limits, recent_context_tokens=1, reactive_recent_context_tokens=1)
        def initial(payload):
            return dict(as_of_message_id=payload["as_of_message_id"],
                        facts=[dict(id="port", content="端口48317", source_refs=["msg-2"], status="active")], dispositions=[])
        self.assertEqual(self.compact(Model(extraction=initial))[0].status, "compacted")
        self.assertIn(correction, self.manager.key_fact_snapshot["excluded_reference_ids"])
        self.manager.append_user_query("继续按更正配置核对")
        self.manager.append_messages([dict(role="assistant", content="新增资料。"*5000)], completes_round=True)
        def updated(payload):
            self.assertIn(correction, [s["message_id"] for s in payload["history_prefix"]])
            return dict(as_of_message_id=payload["as_of_message_id"],
                        facts=[dict(id="port", content="端口59261", source_refs=[correction], status="active")],
                        dispositions=[dict(old_fact_id="port", action="UPDATED", new_fact_id="port", source_refs=[correction])])
        self.assertEqual(self.compact(Model(extraction=updated))[0].status, "compacted")
        self.assertEqual(self.manager.key_fact_snapshot["facts"][0]["content"], "端口59261")

    def test_raw_message_compatibility_keeps_intent_read_only(self):
        manager = ContextManager.from_messages([dict(role="assistant", content="历史"*5000),
                                               dict(role="user", content="当前意图参考")])
        manager.mark_round_complete()
        self.assertEqual(manager.latest_query_reference, "当前意图参考")
        self.assertEqual(manager.intent_source_ids(), ("msg-2",))


if __name__ == "__main__":
    unittest.main()
