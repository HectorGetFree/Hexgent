"""验证 Markdown Memory 的存储、显式写入与新 Query 召回。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context import build_context
from hexgent.context.context_builder import build_context_details
from hexgent.utils.token_estimator import estimate_value_tokens
from hexgent.memory import MemoryRetriever, MemoryStore
from hexgent.memory import RetrievedContextRecord, RetrievedMemory
from hexgent.tool.memory_tools import remember
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.skill import SkillDefinition


class MemoryTest(unittest.TestCase):
    """Memory Markdown 是工作区级权威来源，检索只注入少量相关条目。"""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name)

    def test_explicit_memory_is_readable_and_deduplicated(self) -> None:
        """显式工具重复保存同一事实时合并为一个 Markdown 条目。"""
        store = MemoryStore(self.workdir)
        context = ToolExecutionContext(InMemoryPlanStore(), self.workdir, store)
        first = remember("所有代码注释使用中文", "preference", ["注释", "中文"], context)
        second = remember("所有代码注释使用中文", "preference", ["代码"], context)
        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(len(store.items), 1)
        self.assertIn("items/", store.path.read_text(encoding="utf-8"))
        self.assertTrue((store.items_directory / f"{store.items[0].memory_id}.md").exists())
        self.assertEqual(store.items[0].keywords, ("中文", "代码", "注释"))

    def test_new_query_retrieves_and_injects_memory(self) -> None:
        """新 submit 词法召回跨 Session Memory，并固定进入模型上下文。"""
        store = MemoryStore(self.workdir)
        store.upsert("所有代码注释使用中文", "preference", keywords=["代码", "注释", "中文"], source="explicit")
        engine = AgentEngine(HexgentConfig(), self.workdir, memory_store=store)
        state = engine._build_run_state(RunRequest.submit("请给这个代码补充中文注释"), engine.config)
        self.assertEqual(len(state.relevant_memory), 1)
        self.assertIn("<relevant-memory>", build_context(
            state.tool_context, state.relevant_context, state.relevant_memory,
        ))

    def test_unmatched_constraint_remains_available_within_budget(self) -> None:
        """长期约束即使没有词法命中，也按受限数量保留给新 Query。"""
        store = MemoryStore(self.workdir)
        store.upsert("不得创建工作区之外的文件", "constraint", source="explicit")
        results = MemoryRetriever().retrieve("帮我写一首诗", store.items)
        self.assertEqual(results[0].category, "constraint")

    def test_context_record_suppresses_duplicate_memory_in_instructions(self) -> None:
        """同一事实跨 Session 与工作区重复出现时只保留 Session Record。"""
        record = RetrievedContextRecord(
            "ctx-test", "所有代码注释使用中文", "constraint", (1,), 1.0, ("exact_phrase",),
        )
        memory = RetrievedMemory(
            "mem-test", "所有代码注释使用中文", "preference", 1.0, ("exact_phrase",),
        )
        context = ToolExecutionContext(InMemoryPlanStore(), self.workdir)

        instructions = build_context(context, (record,), (memory,))

        self.assertIn("<relevant-session-context>", instructions)
        self.assertNotIn("<relevant-memory>", instructions)

    def test_runtime_injection_budget_drops_retrieval_before_fixed_context(self) -> None:
        """预算不足时保留系统固定指令，但不让召回内容继续挤占上下文。"""
        record = RetrievedContextRecord(
            "ctx-test", "重要事实" * 100, "fact", (1,), 1.0, ("exact_phrase",),
        )
        memory = RetrievedMemory(
            "mem-test", "长期记忆" * 100, "project", 1.0, ("exact_phrase",),
        )
        context = ToolExecutionContext(InMemoryPlanStore(), self.workdir)
        fixed_tokens = estimate_value_tokens(build_context(context))

        instructions = build_context(
            context,
            (record,),
            (memory,),
            runtime_injection_tokens=fixed_tokens + 1,
        )

        self.assertNotIn("<relevant-session-context>", instructions)
        self.assertNotIn("<relevant-memory>", instructions)

    def test_oversized_active_skill_records_warning_without_truncation(self) -> None:
        """超大 Skill 只产生诊断告警，正文仍完整进入 instructions。"""
        context = ToolExecutionContext(InMemoryPlanStore(), self.workdir)
        active_skill = SkillDefinition(
            "large-skill",
            "用于测试固定上下文预算告警。",
            "必须完整保留的规则。" * 100,
            (),
            "skills/large-skill/SKILL.md",
        )

        result = build_context_details(
            context,
            active_skill=active_skill,
            active_skill_warning_tokens=1,
        )

        self.assertIn("必须完整保留的规则", result.instructions)
        self.assertIn("激活 Skill 正文过大", result.warnings[0])
        self.assertGreater(result.instruction_tokens["active_skill"], 1)

    def test_legacy_single_file_is_migrated_to_authoritative_items(self) -> None:
        """首次加载旧版单文件时，保留事实并生成新的索引与条目文件。"""
        directory = self.workdir / ".hexgent" / "memory"
        directory.mkdir(parents=True)
        directory.joinpath("MEMORY.md").write_text(
            """# Hexgent Memory

## Preferences

### mem-fd61d9e3ee48
- 内容：所有代码注释使用中文
- 关键词：中文、注释
- 来源：用户明确要求
- ContextRecord：无
- 更新时间：2026-09-06T10:00:00+08:00

## Constraints

## Project Knowledge

## Decisions

<!-- hexgent-memory-state: {\"processed_context_record_ids\":[\"ctx-001\"]} -->
""",
            encoding="utf-8",
        )

        store = MemoryStore(self.workdir)

        self.assertEqual(len(store.items), 1)
        item = store.items[0]
        self.assertEqual(item.content, "所有代码注释使用中文")
        self.assertTrue((store.items_directory / f"{item.memory_id}.md").exists())
        self.assertIn(f"items/{item.memory_id}.md", store.path.read_text(encoding="utf-8"))
        self.assertEqual(store.pending_context_record_ids(["ctx-001", "ctx-002"]), ("ctx-002",))


if __name__ == "__main__":
    unittest.main()
