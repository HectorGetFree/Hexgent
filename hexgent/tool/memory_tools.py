"""Memory 领域向 Agent 暴露的显式写入工具。"""

from hexgent.memory.memory_store import MemoryCategory
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_result import ToolHandlerResult


def validate_remember(
    arguments: dict[str, object],
    context: ToolExecutionContext,
) -> str | None:
    """确保模型只能在可用 MemoryStore 中提交简短长期条目。"""
    content = arguments.get("content")
    if not isinstance(content, str) or not content.strip():
        return "Memory content 不能为空"
    if arguments.get("category") not in {"preference", "constraint", "project", "decision"}:
        return "Memory category 无效"
    return None


def remember(
    content: str,
    category: MemoryCategory,
    keywords: list[str],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """保存用户明确要求长期记住的信息。"""
    # 1. 工具只处理显式记忆；自动沉淀由独立的安全边界协调器负责。
    mutation = context.memory_store.upsert(
        content, category, keywords=keywords, source="explicit",
    )

    # 2. 返回稳定标识，供模型确认已记住而无需读取完整 Markdown 文件。
    return ToolHandlerResult.success(
        f"已保存长期记忆：{mutation.item.content}",
        {"memory_id": mutation.item.memory_id, "action": mutation.action},
    )
