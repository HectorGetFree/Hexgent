"""构建每轮模型调用所需的运行时上下文。"""

from hexgent.plan import format_plan_context
from hexgent.prompt import SYSTEM_PROMPT
from hexgent.memory import (
    RetrievedContextRecord,
    RetrievedMemory,
    format_retrieved_context,
    format_retrieved_memory,
)
from hexgent.tool.tool_context import ToolExecutionContext


def build_context(
    context: ToolExecutionContext,
    relevant_context: tuple[RetrievedContextRecord, ...] = (),
    relevant_memory: tuple[RetrievedMemory, ...] = (),
) -> str:
    """组合系统提示词和当前 Plan 运行时状态。"""
    plan_context = format_plan_context(context.plan_store.get_plan())
    retrieved_context = format_retrieved_context(relevant_context)
    retrieved_memory = format_retrieved_memory(relevant_memory)
    sections = [SYSTEM_PROMPT]
    if plan_context:
        sections.append(plan_context)
    if retrieved_context:
        sections.append(retrieved_context)
    if retrieved_memory:
        sections.append(retrieved_memory)
    return "\n\n".join(sections)
