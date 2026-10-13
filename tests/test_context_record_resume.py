"""M3 C5-B 局部修复回归：正式恢复及请求级独立 Oracle，无网络。"""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context.context_compaction import compact_context_if_needed
from hexgent.events import ToolFinishedEvent
from hexgent.memory import ContextRecordInput
from hexgent.run_control import RunControl
from hexgent.session.checkpoint import CheckpointStore
from eval.support.agent_contracts import FakeModel, text_response, tool_call

A = "Vorus-Q9 当前端口为 48317。"
B = "Vorus-Q9 当前端口为 59263。"
C = "Vorus-Q9 当前端口为 60113。"


def record_block(request):
    match = re.search(r"<relevant-session-context>.*?</relevant-session-context>", request["instructions"], re.S)
    if not match:
        return []
    text = match.group(0)
    return json.loads(text[text.index("[{"):text.index("</relevant-session-context>")].strip())


class ContextRecordResumeTest(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = HexgentConfig()
        self.config = replace(base, context_extraction=replace(base.context_extraction, enabled=False),
                              context_retrieval=replace(base.context_retrieval, enabled=True),
                              memory_retrieval=replace(base.memory_retrieval, enabled=False))
        self.engine = AgentEngine(self.config, self.root)
        model = FakeModel([text_response("用户授权已登记")])
        list(self.engine.run("当前事实：" + A + "用户明确授权后续依次更正为：" + B + C,
                             client=SimpleNamespace(responses=model)))
        self.source = next(r["sequence"] for r in self.engine.session.store.read_transcript() if r["type"] == "context_user_query_appended")
        self.old = self.engine.upsert_context_records([ContextRecordInput(A, "fact", (self.source,))])[0].record.record_id
        self.requests = []
        self.checkpoint = None

    def tearDown(self):
        output = os.environ.get("M3_EVIDENCE_DIR")
        if output:
            target = Path(output) / self._testMethodName
            shutil.copytree(self.root, target)
            (target / "oracle-evidence.json").write_text(json.dumps(dict(
                checkpoint=asdict(self.checkpoint) if self.checkpoint else None,
                current_store=[asdict(r) for r in self.engine.session.context_records.records],
                provider_requests=self.requests, record_blocks=[record_block(r) for r in self.requests],
                oracle="PASS" if self._outcome.success else "FAIL",
                evidence_type="DETERMINISTIC_MECHANISM_EVIDENCE", provider_tokens=None,
            ), ensure_ascii=False, default=str), encoding="utf-8")

    def pause(self):
        control = RunControl()
        model = FakeModel([text_response(output=[tool_call("pause", "glob", '{"pattern":"*.absent"}')])])
        for event in self.engine.run("继续 Vorus-Q9 端口任务，先 glob。", client=SimpleNamespace(responses=model), run_control=control):
            if isinstance(event, ToolFinishedEvent):
                control.pause()
        self.assertTrue(self.engine.has_continuable_run)
        commits = [r for r in self.engine.session.store.read_transcript() if r["type"] == "checkpoint_committed"]
        self.checkpoint = CheckpointStore(self.engine.session.store.session_dir).read(commits[-1]["payload"]["checkpoint_id"])
        self.assertEqual([r["record_id"] for r in self.checkpoint.run_state["relevant_context"]], [self.old])

    def correct(self, value, previous=None):
        return self.engine.session.upsert_context_records([ContextRecordInput(value, "fact", (self.source,), (previous or self.old,))])[-1].record.record_id

    def resume(self, responses, config=None):
        identity = self.engine.session.store.session_id
        self.engine = AgentEngine.restore(config or self.config, self.root, identity)
        self.assertTrue(self.engine.has_continuable_run)
        model = FakeModel(responses)
        events = list(self.engine.run(RunRequest.continue_run(), client=SimpleNamespace(responses=model)))
        self.requests = model.calls
        self.assertEqual(self.engine.session.store.session_id, identity)
        self.assertEqual(events[-1].result.status, "completed")
        return model

    def assert_only(self, value):
        for request in self.requests:
            blocks = record_block(request)
            self.assertEqual([r["content"] for r in blocks], [value], "STALE_RECORD_INJECTION/RETRIEVAL_SELECTION_FAILURE")
            active = {r.record_id: r for r in self.engine.session.context_records.active_records}
            for record in blocks:
                self.assertIn(record["record_id"], active, "RESTORE_STATE_MISMATCH")
                self.assertEqual(record["source_sequences"], list(active[record["record_id"]].source_sequences))

    def test_r1_corrected_checkpoint_selects_b(self):
        self.pause()
        self.correct(B)
        self.resume([text_response("59263")])
        self.assert_only(B)

    def test_r2_unchanged_checkpoint_retains_a_once(self):
        self.pause()
        self.resume([text_response("48317")])
        self.assert_only(A)

    def test_r3_chained_supersede_only_c(self):
        self.pause()
        b = self.correct(B)
        self.correct(C, b)
        self.resume([text_response("60113")])
        self.assert_only(C)

    def test_r4_multiple_requests_do_not_revive_a(self):
        self.pause()
        self.correct(B)
        self.resume([text_response(output=[tool_call("one", "glob", '{"pattern":"*.one"}')]),
                     text_response(output=[tool_call("two", "glob", '{"pattern":"*.two"}')]), text_response("59263")])
        self.assertEqual(len(self.requests), 3)
        self.assert_only(B)

    def test_r5_empty_stream_retry_reselects_after_correction(self):
        self.pause()
        self.engine = AgentEngine.restore(self.config, self.root, self.engine.session.store.session_id)
        outer = self
        class RetryClient:
            def __init__(self):
                self.calls = []
                self.responses = self
            def create(self, **kwargs):
                self.calls.append(kwargs)
                if len(self.calls) == 1:
                    def interrupted():
                        outer.correct(B)
                        if False:
                            yield None
                    return interrupted()
                return FakeModel([text_response("59263")]).create(**kwargs)
        client = RetryClient()
        events = list(self.engine.run(RunRequest.continue_run(), client=client))
        self.requests = client.calls
        self.assertEqual(events[-1].result.status, "completed")
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([r["content"] for r in record_block(client.calls[0])], [A])
        self.assertEqual([r["content"] for r in record_block(client.calls[1])], [B], "STALE_RECORD_INJECTION")

    def test_r6_continue_disabled_does_not_inject_cached_record(self):
        self.pause()
        self.correct(B)
        off = replace(self.config, context_retrieval=replace(self.config.context_retrieval, enabled=False))
        self.resume([text_response("继续")], off)
        self.assertEqual(record_block(self.requests[0]), [])

    def test_nonempty_summary_on_off_retains_normal_history(self):
        # 使用真实 Compaction 函数形成非空摘要，不修改 Checkpoint。
        context = self.engine.context_manager
        history = ("项目调研记录：部署前必须完成变更评审，检查维护窗口及负责人。容量评估分别核对CPU、内存与连接池，"
                   "不要把测试环境观测当作生产承载指标。发布采用逐步放量，先检查健康探针，再对照请求错误率与业务校验。"
                   "发生异常时由值班负责人决定回滚，执行前保存配置快照、校验旧版本仍可运行。数据变更需要独立评审兼容性，"
                   "避免代码回滚后无法读取新数据。发布结束记录变更范围、实际时间及遗留事项，交接给下一班负责人。"
                   "这些材料是用于测试摘要历史保留的独立资料，不改变Vorus-Q9已经观察到的端口事实。")
        self.engine.session.record_context_messages(
            [{"role":"assistant", "content":history}],
            completes_round=True, source="model_response")
        context.append_messages([{"role":"assistant", "content":history}], completes_round=True)
        limits = replace(self.config.context, min_compaction_savings_tokens=1, min_compaction_savings_ratio=0.001, recent_context_tokens=1)
        summary = "<summary>历史观察：" + A + "部署还需审批。</summary>"
        from tests.support.fact_extraction_fixture import WithExtraction
        client = SimpleNamespace(responses=WithExtraction(FakeModel([text_response(summary)])))
        stream = compact_context_if_needed(context, client, "offline", "系统", [], limits, force=True)
        while True:
            try:
                next(stream)
            except StopIteration as finished:
                outcome = finished.value
                break
        self.assertIn(outcome.status, {"compacted", "partial"})
        self.engine.session.record_compaction(outcome.boundary)
        snapshot = self.root / "snapshot"
        snapshot.mkdir()
        shutil.copytree(self.root / ".hexgent", snapshot / ".hexgent")
        requests = {}
        for variant in ("ON", "OFF"):
            workdir = self.root / variant
            shutil.copytree(snapshot, workdir)
            cfg = replace(self.config, context_retrieval=replace(self.config.context_retrieval, enabled=variant == "ON"))
            engine = AgentEngine.restore(cfg, workdir, self.engine.session.store.session_id)
            model = FakeModel([text_response("完成")])
            list(engine.run("Vorus-Q9 端口是什么？", client=SimpleNamespace(responses=model)))
            requests[variant] = model.calls[0]
        self.requests = list(requests.values())
        self.assertEqual(requests["ON"]["input"], requests["OFF"]["input"], "EVAL_ISOLATION_FAILURE")
        self.assertIn(A, str(requests["OFF"]["input"]))
        self.assertEqual(record_block(requests["OFF"]), [])
        self.assertEqual([r["content"] for r in record_block(requests["ON"])], [A])
