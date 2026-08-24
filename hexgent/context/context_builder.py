"""构建每轮模型调用所需的运行时上下文。"""

from hexgent.plan import format_plan_context
from hexgent.prompt import SYSTEM_PROMPT
from hexgent.tool.tool_context import ToolExecutionContext


def build_context(context: ToolExecutionContext) -> str:
    """组合系统提示词和当前 Plan 运行时状态。"""
    plan_context = format_plan_context(context.plan_store.get_plan())
    if not plan_context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{plan_context}"
