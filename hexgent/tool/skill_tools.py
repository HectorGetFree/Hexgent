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


def validate_create_skill(
    arguments: dict[str, object],
    _context: ToolExecutionContext,
) -> str | None:
    """校验用户明确沉淀工作流时提供的最小 Skill 定义。"""
    name = arguments.get("name")
    description = arguments.get("description")
    instructions = arguments.get("instructions")
    allowed_tools = arguments.get("allowed_tools")
    if not isinstance(name, str) or not name:
        return "Skill name 不能为空"
    if not isinstance(description, str) or not description.strip():
        return "Skill description 不能为空"
    if not isinstance(instructions, str) or not instructions.strip():
        return "Skill instructions 不能为空"
    if not isinstance(allowed_tools, list) or not all(isinstance(tool, str) for tool in allowed_tools):
        return "Skill allowed_tools 必须是字符串数组"
    public_tools = {tool["name"] for tool in TOOLS}
    unknown_tools = sorted(set(allowed_tools) - public_tools)
    if unknown_tools:
        return f"Skill 声明了未注册工具：{', '.join(unknown_tools)}"
    return None


def create_skill(
    name: str,
    description: str,
    allowed_tools: list[str],
    instructions: str,
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """创建新的项目级 Skill，但不自动改变当前 Run 的激活状态。"""
    try:
        skill = context.skill_registry.create(
            name,
            description,
            tuple(allowed_tools),
            instructions,
        )
    except FileExistsError as error:
        return ToolHandlerResult.failed("business_failure", str(error))
    except (OSError, ValueError) as error:
        return ToolHandlerResult.failed("external_dependency_failed", f"无法创建 Skill：{error}")
    return ToolHandlerResult.success(
        f"已创建项目 Skill：{skill.name}。下一轮可调用 activate_skill 激活。",
        {"skill": skill.to_dict(), "created": True},
    )
