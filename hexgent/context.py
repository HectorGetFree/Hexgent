"""构建每轮模型调用所需的上下文。"""

from hexgent.prompt import SYSTEM_PROMPT
from hexgent.task import format_task_context
from hexgent.tool.tool_context import ToolExecutionContext


def build_context(context: ToolExecutionContext) -> str:
    """组合系统提示词和当前 Task 运行时状态。"""
    task_context = format_task_context(context.task_store.get_task())
    if not task_context:
        return SYSTEM_PROMPT
    return f"{SYSTEM_PROMPT}\n\n{task_context}"
