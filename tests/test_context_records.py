"""验证 Session ContextRecord 的新增、更新、替代与持久化。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.session import (
    ContextRecordInput,
    Session,
    SessionRestorer,
)


class ContextRecordTest(unittest.TestCase):
    """ContextRecord 只保存可追溯的 Session 核心信息。"""

    def test_upsert_persists_projection_and_replays_from_transcript(self) -> None:
        """新增、合并和显式替代均应落盘且可从 Transcript 恢复。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            query = ContextManager().append_user_query("请记住当前项目约定")
            session.record_user_query(query)

            created = session.upsert_context_records(
                [
                    ContextRecordInput(
                        "所有错误信息与注释使用中文。",
                        "constraint",
                        (3,),
                    )
                ]
            )
            record_id = created[0].record.record_id
            updated = session.upsert_context_records(
                [
                    ContextRecordInput(
                        "所有错误信息与注释使用中文。",
                        "constraint",
                        (4,),
                    )
                ]
            )
            replaced = session.upsert_context_records(
                [
                    ContextRecordInput(
                        "所有新代码的注释使用中文；固定接口标识保持原样。",
                        "decision",
                        (5,),
                        (record_id,),
                    )
                ]
            )
            projection = json.loads(
                session.store.context_records_path.read_text(encoding="utf-8")
            )
            restored = SessionRestorer(root).restore(session.store.session_id)

        self.assertEqual([mutation.action for mutation in created], ["created"])
        self.assertEqual([mutation.action for mutation in updated], ["updated"])
        self.assertEqual(
            [mutation.action for mutation in replaced],
            ["superseded", "created"],
        )
        self.assertEqual(projection["schema_version"], 2)
        self.assertEqual(len(projection["records"]), 2)
        self.assertEqual(len(restored.session.context_records.records), 2)
        self.assertEqual(len(restored.session.context_records.active_records), 1)
        old_record = next(
            record
            for record in restored.session.context_records.records
            if record.record_id == record_id
        )
        self.assertEqual(old_record.source_sequences, (3, 4))
        self.assertEqual(old_record.status, "superseded")

    def test_rejects_source_sequence_that_is_not_yet_a_transcript_fact(self) -> None:
        """Session 入口后续会把条目绑定到已经落盘的安全事实。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            query = ContextManager().append_user_query("建立证据边界")
            session.record_user_query(query)

            with self.assertRaisesRegex(ValueError, "尚未落盘"):
                session.upsert_context_records(
                    [ContextRecordInput("未落盘事实", "fact", (99,))]
                )

    def test_engine_exposes_session_record_submission_at_safe_boundary(self) -> None:
        """外部入口应通过 Engine 访问记录，而不是直接操作 Session 内部。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = AgentEngine.create(HexgentConfig(), root)
            query = engine.context_manager.append_user_query("记录一条项目事实")
            engine.session.record_user_query(query)

            mutations = engine.upsert_context_records(
                [ContextRecordInput("当前工作区是 Hexgent 项目。", "fact", (3,))]
            )

        self.assertEqual([mutation.action for mutation in mutations], ["created"])


if __name__ == "__main__":
    unittest.main()
