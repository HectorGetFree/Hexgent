"""构建每轮模型调用所需的运行时上下文。"""

from dataclasses import dataclass
from hashlib import sha256
import json

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


@dataclass(frozen=True)
class BuiltContext:
    """一次模型调用的 instructions 与可审计的分项 Token 估算。"""

    # 最终传入模型的完整 instructions。
    instructions: str

    # System Prompt、Plan、Skill、召回内容各自的估算 Token。
    instruction_tokens: dict[str, int]

    # 不截断正文、仅供 Session 记录的超大固定区块告警。
    warnings: tuple[str, ...]

    # 去重和预算筛选后的最终 Memory 凭证；不保存正文。
    memory_receipt: tuple[dict[str, str], ...] = ()


def build_context(
    context: ToolExecutionContext,
    relevant_context: tuple[RetrievedContextRecord, ...] = (),
    relevant_memory: tuple[RetrievedMemory, ...] = (),
    active_skill: SkillDefinition | None = None,
    runtime_injection_tokens: int | None = None,
) -> str:
    """组合系统提示词和受统一预算约束的运行时上下文。"""
    return build_context_details(
        context,
        relevant_context,
        relevant_memory,
        active_skill,
        runtime_injection_tokens,
    ).instructions


def build_context_details(
    context: ToolExecutionContext,
    relevant_context: tuple[RetrievedContextRecord, ...] = (),
    relevant_memory: tuple[RetrievedMemory, ...] = (),
    active_skill: SkillDefinition | None = None,
    runtime_injection_tokens: int | None = None,
    *,
    plan_warning_tokens: int | None = None,
    active_skill_warning_tokens: int | None = None,
    pinned_constraints: tuple[dict, ...] = (),
    pinned_constraints_tokens: int = 4_000,
) -> BuiltContext:
    """构建 instructions，并返回分项预算快照供运行时审计使用。"""
    plan_context = format_plan_context(context.plan_store.get_plan())
    # Session Record 优先于同内容的工作区 Memory，避免同一事实重复占用请求预算。
    deduplicated_memory = _without_context_duplicates(
        relevant_context, context.memory_store.revalidate(relevant_memory),
    )
    sections = [SYSTEM_PROMPT]
    fixed_tokens = {"system_prompt": estimate_value_tokens(SYSTEM_PROMPT)}
    pinned_constraints = tuple(item for item in pinned_constraints if item["status"] == "ACTIVE")
    if pinned_constraints:
        # JSON 中转义尖括号，原文不能关闭保护区；不合并不同 ID。
        body = json.dumps(list(pinned_constraints), ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
        block = "<pinned-constraints>\n" + body + "\n</pinned-constraints>"
        tokens = estimate_value_tokens(block)
        if tokens > pinned_constraints_tokens:
            raise ValueError("PINNED_CONSTRAINTS_LIMIT_EXCEEDED")
        sections.append(block)
        fixed_tokens["pinned_constraints"] = tokens
    warnings: list[str] = []
    if plan_context:
        sections.append(plan_context)
        fixed_tokens["plan"] = estimate_value_tokens(plan_context)
        if (
            plan_warning_tokens is not None
            and fixed_tokens["plan"] > plan_warning_tokens
        ):
            warnings.append("Plan 正文过大，可能挤占近期对话上下文")
    if active_skill is not None:
        active_skill_context = (
            "<active-skill\n"
            f'name="{active_skill.name}"\n'
            f'path="{active_skill.relative_path}">\n'
            f"{active_skill.instructions}\n"
            "</active-skill>"
        )
        sections.append(active_skill_context)
        fixed_tokens["active_skill"] = estimate_value_tokens(active_skill_context)
        if (
            active_skill_warning_tokens is not None
            and fixed_tokens["active_skill"] > active_skill_warning_tokens
        ):
            warnings.append("激活 Skill 正文过大，可能挤占近期对话上下文")
    else:
        # 激活后完整 Skill 已进入上下文，重复目录只会浪费 Token。
        skill_catalog = context.skill_registry.catalog()
        if skill_catalog:
            sections.append(skill_catalog)
            fixed_tokens["skill_catalog"] = estimate_value_tokens(skill_catalog)

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
    if retrieved_context:
        fixed_tokens["context_records"] = estimate_value_tokens(retrieved_context)
    if retrieved_memory:
        fixed_tokens["memory"] = estimate_value_tokens(retrieved_memory)
    instructions = "\n\n".join(sections)
    fixed_tokens["instructions_total"] = estimate_value_tokens(instructions)
    receipt = tuple({
        "memory_id": item.memory_id,
        "category": item.category,
        "content_sha256": sha256(item.content.encode("utf-8")).hexdigest(),
    } for item in selected_memory)
    return BuiltContext(instructions, fixed_tokens, tuple(warnings), receipt)


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
