"""Memory 领域向 Agent 暴露的显式写入工具。"""

import re
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
    if len(" ".join(content.split())) > 500:
        return "Memory content 超过 500 字符"
    keywords = arguments.get("keywords", [])
    if (not isinstance(keywords, list) or len(keywords) > 12
            or any(not isinstance(k, str) or len(k) > 64 for k in keywords)):
        return "Memory keywords 无效"
    return None


def validate_lifecycle(arguments: dict[str, object], context: ToolExecutionContext) -> str | None:
    identity = arguments.get("old_memory_id", arguments.get("memory_id"))
    if not isinstance(identity, str) or re.fullmatch(r"mem-[a-f0-9]{12}", identity) is None:
        return "Memory 标识无效"
    return validate_remember(arguments, context) if "old_memory_id" in arguments else None


def remember(
    content: str,
    category: MemoryCategory,
    keywords: list[str],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """保存用户明确要求长期记住的信息。"""
    # 1. 工具只处理显式记忆；自动沉淀由独立的安全边界协调器负责。
    try:
        mutation = context.memory_store.upsert(
            content, category, keywords=keywords, source="explicit",
        )
    except ValueError as error:
        return _lifecycle_failure(error)

    # 2. 返回稳定标识，供模型确认已记住而无需读取完整 Markdown 文件。
    return ToolHandlerResult.success(
        f"已保存长期记忆：{mutation.item.content}",
        {"memory_id": mutation.item.memory_id, "action": mutation.action},
    )


def supersede_memory(old_memory_id: str, content: str, category: MemoryCategory,
                     keywords: list[str], context: ToolExecutionContext) -> ToolHandlerResult:
    """显式替代当前 Scope 的旧事实，不自动识别冲突。"""
    try:
        mutation = context.memory_store.supersede(old_memory_id, content, category, keywords=keywords)
    except ValueError as error:
        return _lifecycle_failure(error)
    return ToolHandlerResult.success("长期记忆已替代", {"memory_id": mutation.item.memory_id, "action": mutation.action})


def invalidate_memory(memory_id: str, context: ToolExecutionContext) -> ToolHandlerResult:
    """保留正文但禁止后续长期 Memory 注入。"""
    try:
        item = context.memory_store.invalidate(memory_id)
    except ValueError as error:
        return _lifecycle_failure(error)
    return ToolHandlerResult.success("长期记忆已失效", {"memory_id": memory_id, "status": item.status})


def delete_memory(memory_id: str, context: ToolExecutionContext) -> ToolHandlerResult:
    """删除正文并保留防复活墓碑。"""
    try:
        context.memory_store.delete(memory_id)
    except ValueError as error:
        return _lifecycle_failure(error)
    except OSError:
        return ToolHandlerResult.failed("MEMORY_DELETE_INCOMPLETE", "删除未完整完成，请核验权威状态并重试清理", side_effect_state="unknown")
    return ToolHandlerResult.success("长期记忆已删除", {"memory_id": memory_id, "deleted": True})


def _lifecycle_failure(error: ValueError) -> ToolHandlerResult:
    code = str(error)
    messages = {"MEMORY_NOT_FOUND": "当前工作区不存在该记忆", "MEMORY_REVOKED": "记忆已撤销，不能隐式复活",
                "MEMORY_LIFECYCLE_CONFLICT": "记忆生命周期冲突"}
    return ToolHandlerResult.failed(code if code in messages else "MEMORY_INVALID_INPUT", messages.get(code, str(error)))
