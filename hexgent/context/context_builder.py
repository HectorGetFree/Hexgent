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
from hexgent.skill import SkillDefinition
from hexgent.utils.token_estimator import estimate_value_tokens


def build_context(
    context: ToolExecutionContext,
    relevant_context: tuple[RetrievedContextRecord, ...] = (),
    relevant_memory: tuple[RetrievedMemory, ...] = (),
    active_skill: SkillDefinition | None = None,
    runtime_injection_tokens: int | None = None,
) -> str:
    """组合系统提示词和受统一预算约束的运行时上下文。"""
    plan_context = format_plan_context(context.plan_store.get_plan())
    # Session Record 优先于同内容的工作区 Memory，避免同一事实重复占用请求预算。
    deduplicated_memory = _without_context_duplicates(
        relevant_context, relevant_memory,
    )
    sections = [SYSTEM_PROMPT]
    if plan_context:
        sections.append(plan_context)
    if active_skill is not None:
        sections.append(
            "<active-skill\n"
            f'name="{active_skill.name}"\n'
            f'path="{active_skill.relative_path}">\n'
            f"{active_skill.instructions}\n"
            "</active-skill>"
        )
    else:
        # 激活后完整 Skill 已进入上下文，重复目录只会浪费 Token。
        skill_catalog = context.skill_registry.catalog()
        if skill_catalog:
            sections.append(skill_catalog)

    selected_context, selected_memory = _fit_retrieval_budget(
        relevant_context,
        deduplicated_memory,
        "\n\n".join(sections),
        runtime_injection_tokens,
    )
    retrieved_context = format_retrieved_context(selected_context)
    retrieved_memory = format_retrieved_memory(selected_memory)
    if retrieved_context:
        sections.append(retrieved_context)
    if retrieved_memory:
        sections.append(retrieved_memory)
    return "\n\n".join(sections)


def _without_context_duplicates(
    records: tuple[RetrievedContextRecord, ...],
    memories: tuple[RetrievedMemory, ...],
) -> tuple[RetrievedMemory, ...]:
    """移除与已召回 Session Record 内容等价的长期 Memory。"""
    record_contents = {_normalize_recalled_content(item.content) for item in records}
    return tuple(
        item
        for item in memories
        if _normalize_recalled_content(item.content) not in record_contents
    )


def _normalize_recalled_content(content: str) -> str:
    """只做稳定的文本规范化，不在组装阶段推断两条不同事实是否语义相同。"""
    return " ".join(content.casefold().split())


def _fit_retrieval_budget(
    records: tuple[RetrievedContextRecord, ...],
    memories: tuple[RetrievedMemory, ...],
    fixed_context: str,
    budget_tokens: int | None,
) -> tuple[tuple[RetrievedContextRecord, ...], tuple[RetrievedMemory, ...]]:
    """在统一注入预算内选择召回项，Session Record 优先于长期 Memory。"""
    if budget_tokens is None:
        return records, memories
    remaining = budget_tokens - estimate_value_tokens(fixed_context)
    if remaining <= 0:
        return (), ()

    selected_records: list[RetrievedContextRecord] = []
    selected_memories: list[RetrievedMemory] = []
    for record in records:
        candidate = tuple([*selected_records, record])
        if _retrieval_tokens(candidate, tuple(selected_memories)) <= remaining:
            selected_records.append(record)
    for memory in memories:
        candidate = tuple([*selected_memories, memory])
        if _retrieval_tokens(tuple(selected_records), candidate) <= remaining:
            selected_memories.append(memory)
    return tuple(selected_records), tuple(selected_memories)


def _retrieval_tokens(
    records: tuple[RetrievedContextRecord, ...],
    memories: tuple[RetrievedMemory, ...],
) -> int:
    """按最终封装格式估算两类召回内容的联合 Token 占用。"""
    return estimate_value_tokens(
        "\n\n".join(
            part
            for part in (
                format_retrieved_context(records),
                format_retrieved_memory(memories),
            )
            if part
        )
    )
