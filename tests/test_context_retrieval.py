"""验证 ContextRecord 词法召回、注入和 AgentEngine 主流程。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager, build_context
from hexgent.loop.loop_state import RunStateSnapshot
from hexgent.memory import (
    ContextRecordInput,
    ContextRecordRetriever,
    ContextRecordStore,
    ContextRetrievalLimits,
)


class ContextRetrievalTest(unittest.TestCase):
    """召回结果应可解释、受预算约束且固定于当前 Run。"""

    def test_retrieves_lexical_match_and_keeps_active_constraint(self) -> None:
        """代码标识符和中文片段应命中，未命中约束仍可少量保留。"""
        store = ContextRecordStore()
        store.upsert(
            [
                ContextRecordInput(
                    "SessionRestorer 负责通过 Checkpoint 恢复会话状态。",
                    "fact",
                    (3,),
                ),
                ContextRecordInput(
                    "所有错误信息和代码注释使用中文。",
                    "constraint",
                    (4,),
                ),
                ContextRecordInput(
                    "前端采用 React。",
                    "decision",
                    (5,),
                ),
            ]
        )

        results = ContextRecordRetriever().retrieve(
            "继续检查 SessionRestorer 的会话恢复逻辑",
            store.active_records,
        )

        self.assertEqual([result.category for result in results], ["fact", "constraint"])
        self.assertIn("token_overlap", results[0].match_reasons[0])
        self.assertEqual(results[1].match_reasons, ("active_constraint",))

    def test_respects_record_and_character_budget(self) -> None:
        """召回不能因为匹配数量增加而突破本次 Run 注入预算。"""
        store = ContextRecordStore()
        store.upsert(
            [
                ContextRecordInput("恢复方案甲" * 10, "fact", (1,)),
                ContextRecordInput("恢复方案乙" * 10, "fact", (2,)),
            ]
        )
        retriever = ContextRecordRetriever(
            ContextRetrievalLimits(max_records=1, max_characters=100)
        )

        results = retriever.retrieve("恢复方案", store.active_records)

        self.assertEqual(len(results), 1)
        self.assertLessEqual(sum(len(result.content) for result in results), 100)

    def test_engine_retrieves_records_and_context_builder_injects_them(self) -> None:
        """新 Query 应在调用 Agent Loop 前完成召回并注入 instructions。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = AgentEngine.create(HexgentConfig(), root)
            source_query = engine.context_manager.append_user_query("确认恢复设计")
            engine.session.record_user_query(source_query)
            engine.upsert_context_records(
                [
                    ContextRecordInput(
                        "Session 恢复采用 Checkpoint 加 Transcript 增量重放。",
                        "decision",
                        (3,),
                    )
                ]
            )

            with patch("hexgent.agent_engine.agent_loop", return_value=iter(())) as loop:
                list(engine.run("继续优化 Session 的恢复设计"))

            (state,) = loop.call_args.args
            instructions = build_context(state.tool_context, state.relevant_context)
            records = engine.session.store.read_transcript()

        self.assertEqual(len(state.relevant_context), 1)
        self.assertIn("relevant-session-context", instructions)
        self.assertIn("Checkpoint 加 Transcript", instructions)
        retrieval_events = [
            record for record in records if record["type"] == "context_records_retrieved"
        ]
        self.assertEqual(len(retrieval_events), 1)
        self.assertEqual(
            retrieval_events[0]["payload"]["records"][0]["record_id"],
            state.relevant_context[0].record_id,
        )

        restored_snapshot = RunStateSnapshot.from_dict(
            state.to_snapshot().to_dict()
        )
        self.assertEqual(
            restored_snapshot.relevant_context,
            state.relevant_context,
        )


if __name__ == "__main__":
    unittest.main()
