"""验证项目级 Skill 的发现、激活与可恢复状态。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.context import ContextManager, build_context
from hexgent.loop.agent_turn import _activated_skill, _available_tools_for_state
from hexgent.loop.loop_state import LoopState, RunStateSnapshot
from hexgent.plan import InMemoryPlanStore
from hexgent.skill import SkillRegistry
from hexgent.tool.skill_tools import activate_skill, validate_activate_skill
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_scheduler import CompletedToolCall
from hexgent.tool.tool_result import ToolExecutionResult


class SkillTest(unittest.TestCase):
    """第一版只支持项目级目录式 SKILL.md。"""

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.workdir = Path(self.directory.name)
        skill_path = self.workdir / ".hexgent" / "skills" / "code-review" / "SKILL.md"
        skill_path.parent.mkdir(parents=True)
        skill_path.write_text(
            """---
name: code-review
description: 基于源码证据审查项目设计。
allowed_tools:
  - glob
  - read_file
---
# 代码审查

先枚举目标文件，再逐一读取实现和测试；所有结论必须引用已读取的源码证据。
""",
            encoding="utf-8",
        )

    def test_registry_only_injects_catalog_before_activation(self) -> None:
        """未激活时上下文只能看到目录，不应携带完整 Skill 正文。"""
        context = ToolExecutionContext(InMemoryPlanStore(), self.workdir)

        instructions = build_context(context)

        self.assertIn("<available-skills>", instructions)
        self.assertIn("code-review: 基于源码证据审查项目设计。", instructions)
        self.assertNotIn("所有结论必须引用", instructions)

    def test_activation_updates_next_state_and_restricts_tools(self) -> None:
        """成功工具结果才可激活 Skill，并在下一轮收紧模型可见工具。"""
        context = ToolExecutionContext(InMemoryPlanStore(), self.workdir)
        result = activate_skill("code-review", context)
        completed = CompletedToolCall(
            "call-skill", "activate_skill",
            ToolExecutionResult("success", result.output, False, data=result.data),
        )
        active = _activated_skill(None, (completed,))
        assert active is not None
        state = LoopState(ContextManager(), context, active_skill=active)

        instructions = build_context(context, active_skill=active)
        tool_names = {tool["name"] for tool in _available_tools_for_state(state)}

        self.assertIn("<active-skill", instructions)
        self.assertIn("所有结论必须引用", instructions)
        self.assertEqual(tool_names, {"glob", "read_file", "activate_skill"})

    def test_active_skill_survives_checkpoint_snapshot(self) -> None:
        """继续同一 Run 时必须使用激活时冻结的说明，不重新读取文件。"""
        registry = SkillRegistry(self.workdir)
        active = registry.get("code-review")
        assert active is not None
        state = LoopState(
            ContextManager(),
            ToolExecutionContext(InMemoryPlanStore(), self.workdir),
            active_skill=active,
        )

        restored = RunStateSnapshot.from_dict(state.to_snapshot().to_dict())

        self.assertEqual(restored.active_skill, active)

    def test_unknown_declared_tool_is_rejected_before_activation(self) -> None:
        """Skill 白名单不能写入不存在工具并静默造成错误工作流。"""
        skill_path = self.workdir / ".hexgent" / "skills" / "code-review" / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8").replace("  - glob", "  - nonexistent_tool"),
            encoding="utf-8",
        )
        context = ToolExecutionContext(
            InMemoryPlanStore(), self.workdir, None, SkillRegistry(self.workdir),
        )

        error = validate_activate_skill({"name": "code-review"}, context)

        self.assertEqual(error, "Skill 声明了未注册工具：nonexistent_tool")
