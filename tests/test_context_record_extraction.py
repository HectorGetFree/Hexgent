"""验证记录提取来源边界、失败隔离和持久化恢复，不调用真实模型。"""

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.config import HexgentConfig, RunOptions, load_config
from hexgent.agent_engine import AgentEngine
from hexgent.context import ContextManager
from hexgent.events import AgentLoopResult
import hexgent.loop.agent_loop as agent_loop_module
from hexgent.loop.extract_context_records import extract_context_records
from hexgent.loop.loop_state import LoopState
from hexgent.loop.transitions import Stop
from hexgent.memory import ContextRecordInput
from hexgent.memory.context_record_extraction import (
    ContextExtractionLimits, prepare_extraction, parse_candidates,
)
from hexgent.run_control import InterruptController
from hexgent.session import CheckpointStore, Session, SessionRestorer


class ContextRecordExtractionTest(unittest.TestCase):
    """候选只有经过来源校验才能成为可恢复记录。"""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.session = Session.persistent(self.root)
        self.context = ContextManager()
        self.session.record_user_query(self.context.append_user_query("所有注释使用中文"))
        self.query_sequence = self.session.store.next_sequence - 1
        self.session.record_context_messages(
            [{"role": "assistant", "content": "已了解"}], completes_round=True, source="model_response",
        )
        self.through = self.session.store.next_sequence - 1
        self.limits = ContextExtractionLimits(enabled=True)

    def candidate_text(self, **overrides):
        """提供可核对用户来源的模拟模型输出。"""
        row = {"content": "所有注释使用中文", "category": "constraint",
               "source_sequences": [self.query_sequence], "supersedes": [],
               "evidence_quote": "所有注释使用中文"}
        row.update(overrides)
        return json.dumps({"records": [row]}, ensure_ascii=False)

    def batch(self):
        """只从测试会话已落盘的事实构建提取输入。"""
        return prepare_extraction(self.session.store.read_transcript(), (), self.limits, force=True)

    def test_interval_budget_and_unclosed_tail(self):
        """频率限制生效，未闭合的新轮次不进入输入且预览受预算约束。"""
        records = self.session.store.read_transcript()
        self.assertIsNone(prepare_extraction(records, (), self.limits))
        self.session.record_user_query(self.context.append_user_query("未闭合输入" * 2000))
        batch = self.batch()
        self.assertEqual(batch.through_sequence, self.through)
        self.assertNotIn("未闭合输入", batch.model_input())
        self.assertLessEqual(len(batch.model_input()), self.limits.max_input_characters)

    def test_source_and_quote_validation(self):
        """拒绝未来证据、模型自述伪装的约束和没有原文的候选。"""
        batch = self.batch()
        self.assertEqual(len(parse_candidates(self.candidate_text(), batch, self.limits)), 1)
        for overrides in ({"source_sequences": [999]}, {"evidence_quote": "不存在的要求"},
                          {"source_sequences": [self.through], "evidence_quote": "已了解"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                parse_candidates(self.candidate_text(**overrides), batch, self.limits)

    def test_commit_and_empty_commit_restore_together(self):
        """提取记录及游标一次提交，空结果也推进并可恢复。"""
        self.session.upsert_context_records(
            parse_candidates(self.candidate_text(), self.batch(), self.limits),
            processed_sequence=self.through,
        )
        self.session.record_context_messages([], completes_round=True, source="model_response")
        next_through = self.session.store.next_sequence - 1
        self.session.upsert_context_records([], processed_sequence=next_through)
        restored = SessionRestorer(self.root).restore(self.session.store.session_id)
        self.assertEqual(
            restored.session.last_extracted_transcript_sequence,
            next_through,
        )
        self.assertEqual(len(restored.session.context_records.active_records), 1)

    def test_failed_commit_does_not_mutate_memory(self):
        """事实日志写入失败时，内存记录和处理进度都不能提前改变。"""
        with patch.object(self.session.store, "append_transcript", side_effect=OSError("模拟写入失败")):
            with self.assertRaises(OSError):
                self.session.upsert_context_records(
                    [ContextRecordInput("所有注释使用中文", "constraint", (self.query_sequence,))],
                    processed_sequence=self.through,
                )
        self.assertEqual(self.session.last_extracted_transcript_sequence, 0)
        self.assertEqual(self.session.context_records.records, ())

    def fake_client(self, output):
        """模拟完整 Responses 流，不建立网络连接。"""
        response = SimpleNamespace(output_text=output, usage={"input_tokens": 12, "output_tokens": 4})
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter([
            SimpleNamespace(type="response.completed", response=response),
        ])))
        return client

    def test_extraction_commits_and_accounts_usage(self):
        """完整响应校验成功后才提交候选，提取消耗可独立统计。"""
        with patch("hexgent.loop.extract_context_records.get_model", return_value="模拟模型"):
            usage = extract_context_records(self.session, self.fake_client(self.candidate_text()),
                self.limits, InterruptController(), force=True)
        self.assertEqual(usage, (12, 4))
        self.assertEqual(
            self.session.last_extracted_transcript_sequence,
            self.through,
        )
        self.assertEqual(len(self.session.context_records.active_records), 1)

    def test_invalid_output_and_interrupt_preserve_progress(self):
        """无效模型输出不推进进度，已暂停时不发起提取请求。"""
        with patch("hexgent.loop.extract_context_records.get_model", return_value="模拟模型"):
            extract_context_records(self.session, self.fake_client("垃圾输出"),
                self.limits, InterruptController(), force=True)
        self.assertEqual(self.session.last_extracted_transcript_sequence, 0)
        controller = InterruptController()
        controller.interrupt()
        self.assertEqual(extract_context_records(self.session, None, self.limits, controller, force=True), (0, 0))

    def test_toml_and_run_override(self):
        """提取配置支持 TOML 加载和单次覆盖。"""
        path = self.root / "hexgent.toml"
        path.write_text("[context_extraction]\nenabled = false\nturn_interval = 5\n", encoding="utf-8")
        config = load_config(self.root)
        self.assertFalse(config.context_extraction.enabled)
        engine = AgentEngine(config, self.root, session=Session())
        override = replace(config.context_extraction, enabled=True)
        self.assertIs(engine.resolve_config(RunOptions(context_extraction=override)).context_extraction, override)

    def test_loop_does_not_block_normal_finish_for_extraction(self):
        """正常结束不应为可延后的记录提取额外阻塞用户响应。"""
        engine = AgentEngine(HexgentConfig(), self.root, session=Session())
        state = LoopState(context_manager=self.context, tool_context=engine.tool_context)

        def finished_turn(*_args, **_kwargs):
            if False:
                yield None
            return Stop(AgentLoopResult("完成", 0.0), state)

        with patch.object(agent_loop_module, "run_turn", finished_turn), patch.object(
            agent_loop_module, "extract_context_records"
        ) as extraction:
            outcome = list(agent_loop_module.agent_loop(
                state,
                config=HexgentConfig(),
                client=object(),
                session=self.session,
            ))

        extraction.assert_not_called()
        self.assertEqual(outcome[-1].result.status, "completed")
        checkpoint = CheckpointStore(self.session.store.session_dir).read("cp-000001")
        self.assertEqual(checkpoint.run_state["input_tokens"], 0)
        self.assertEqual(checkpoint.run_state["output_tokens"], 0)
        report = json.loads(
            (self.session.store.session_dir / "reports" / "run-001.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["usage"]["context_extraction"], {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        })

    def test_failed_extraction_is_suppressed_until_next_run(self):
        """同一 Run 内失败后不能反复阻塞相同事实批次。"""
        with patch("hexgent.loop.extract_context_records.get_model", return_value="模拟模型"):
            extract_context_records(
                self.session,
                SimpleNamespace(responses=SimpleNamespace(create=lambda **_kwargs: iter(()))),
                self.limits,
                InterruptController(),
                force=True,
            )
        self.assertTrue(self.session.context_record_extraction_suppressed)

        client = self.fake_client(self.candidate_text())
        self.assertEqual(
            extract_context_records(
                self.session, client, self.limits, InterruptController(), force=True,
            ),
            (0, 0),
        )


if __name__ == "__main__":
    unittest.main()
