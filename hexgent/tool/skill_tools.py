"""Skill 领域向 Agent 暴露的显式激活工具。"""

from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_result import ToolHandlerResult
from hexgent.prompt import TOOLS


def validate_activate_skill(
    arguments: dict[str, object],
    context: ToolExecutionContext,
) -> str | None:
    """只允许激活已发现的项目级 Skill。"""
    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        return "Skill name 不能为空"
    if context.skill_registry.get(name) is None:
        return f"未找到项目 Skill：{name}"
    definition = context.skill_registry.get(name)
    assert definition is not None
    public_tools = {tool["name"] for tool in TOOLS}
    unknown_tools = sorted(set(definition.allowed_tools) - public_tools)
    if unknown_tools:
        return f"Skill 声明了未注册工具：{', '.join(unknown_tools)}"
    return None


def activate_skill(name: str, context: ToolExecutionContext) -> ToolHandlerResult:
    """返回冻结的 Skill 快照，由 Agent Turn 将其写入下一轮 LoopState。"""
    definition = context.skill_registry.get(name)
    if definition is None:
        return ToolHandlerResult.failed("business_failure", f"未找到项目 Skill：{name}")
    skill = definition
    return ToolHandlerResult.success(
        f"已激活 Skill：{skill.name}。下一轮将按其工作流执行。",
        {"skill": skill.to_dict()},
    )
