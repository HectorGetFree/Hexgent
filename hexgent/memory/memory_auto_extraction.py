"""从已验证的 ContextRecord 自动筛选跨 Session Memory。"""

from dataclasses import dataclass
import json
from typing import Any

from .context_records import ContextRecord
from .memory_store import MemoryCategory, MemoryStore
from .memory_prompts import AUTO_MEMORY_EXTRACTION_INSTRUCTIONS as AUTO_MEMORY_INSTRUCTIONS


@dataclass(frozen=True)
class MemoryAutoExtractionLimits:
    """自动 Memory 沉淀的频率、输入和输出边界。"""

    # 是否启用从 ContextRecord 自动筛选长期 Memory。
    enabled: bool = True
    # 单次最多交给模型判断的未处理 ContextRecord 数量。
    max_source_records: int = 12
    # 单次模型最多返回的 Memory 数量。
    max_memories: int = 4
    # 自动提取模型的最大输出 Token。
    max_output_tokens: int = 800
    # 自动提取模型的独立超时秒数。
    timeout_seconds: float = 12.0


def build_auto_memory_input(
    records: tuple[ContextRecord, ...],
    store: MemoryStore,
    limits: MemoryAutoExtractionLimits,
) -> tuple[tuple[ContextRecord, ...], str] | None:
    """筛选尚未判断过的稳定 Record，并构建受限模型输入。"""
    if not limits.enabled:
        return None
    pending_ids = set(store.pending_context_record_ids(record.record_id for record in records))
    pending = tuple(record for record in records if record.record_id in pending_ids)[:limits.max_source_records]
    if not pending:
        return None
    payload = [
        {"record_id": record.record_id, "content": record.content, "category": record.category}
        for record in pending
    ]
    return pending, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def commit_auto_memories(
    text: str,
    source_records: tuple[ContextRecord, ...],
    store: MemoryStore,
    limits: MemoryAutoExtractionLimits,
) -> None:
    """严格校验模型候选后写入 Markdown，并推进已判断来源。"""
    try:
        payload = json.loads(text)
        candidates = payload["memories"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("自动 Memory 输出不是有效 JSON 对象") from error
    if not isinstance(candidates, list) or len(candidates) > limits.max_memories:
        raise ValueError("自动 Memory 数量无效")
    allowed_sources = {record.record_id for record in source_records}
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("自动 Memory 候选必须是对象")
        if set(candidate) != {"content", "category", "keywords", "source_record_ids"}:
            raise ValueError("自动 Memory 候选字段无效")
        category = candidate["category"]
        source_ids = candidate["source_record_ids"]
        if category not in {"preference", "constraint", "project", "decision"}:
            raise ValueError("自动 Memory category 无效")
        if not isinstance(source_ids, list) or not source_ids or not set(source_ids) <= allowed_sources:
            raise ValueError("自动 Memory 来源不属于本次已验证 Record")
        if not isinstance(candidate["keywords"], list) or not all(isinstance(value, str) for value in candidate["keywords"]):
            raise ValueError("自动 Memory keywords 无效")
        store.upsert(
            candidate["content"], category, keywords=candidate["keywords"],
            source="automatic", source_record_ids=source_ids,
        )
    store.mark_context_records_processed(record.record_id for record in source_records)
