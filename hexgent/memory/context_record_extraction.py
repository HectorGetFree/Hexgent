"""从已落盘的增量事实构建受预算约束的 ContextRecord 提取输入。"""

from dataclasses import asdict, dataclass
import json
from typing import Any

from .context_records import ContextRecord, ContextRecordInput
from .memory_prompts import CONTEXT_RECORD_EXTRACTION_INSTRUCTIONS as EXTRACTION_INSTRUCTIONS


@dataclass(frozen=True)
class ContextExtractionLimits:
    """控制自动提取频率、请求规模和等待时间。"""

    # 是否在完整 Turn 边界自动沉淀记录。
    enabled: bool = False
    # 积累多少个未处理完整 Turn 后执行一次提取。
    turn_interval: int = 4
    # 每次请求的事实正文与已有记录合计最大字符数。
    max_input_characters: int = 8_000
    # 单条 Transcript 事件最多展示的正文字符数。
    max_event_characters: int = 1_000
    # 每次最多创建或更新的候选记录数。
    max_records: int = 4
    # 提取模型的最大输出 Token 数。
    max_output_tokens: int = 1_000
    # 提取请求的网络超时；请求不做 SDK 自动重试。
    timeout_seconds: float = 8.0


@dataclass(frozen=True)
class ExtractionBatch:
    """一次提取可引用的事实及其确定性处理进度。"""

    # 只包含已经在完整 Turn 边界之前落盘的输入事件。
    facts: tuple[dict[str, Any], ...]
    # 本批已展示或明确截断处理的最后一条 Transcript 序号。
    through_sequence: int
    # 可被候选显式替代的已有有效记录。
    existing: tuple[ContextRecord, ...]

    def model_input(self) -> str:
        """保留来源和截断标记，以 JSON 数据形式交给提取模型。"""
        return json.dumps({
            "facts": self.facts,
            "existing_records": [asdict(record) for record in self.existing],
        }, ensure_ascii=False)


def prepare_extraction(
    records: list[dict[str, Any]],
    existing: tuple[ContextRecord, ...],
    limits: ContextExtractionLimits,
    *,
    force: bool = False,
) -> ExtractionBatch | None:
    """选择安全边界内的连续增量，截断预览但不引用未展示事实。"""
    # 1. 只有模型消息和工具结果已完整回填的事件才构成安全边界。
    boundaries = [record["sequence"] for record in records if
        record["type"] == "context_messages_appended"
        and record["payload"].get("completes_round") is True]
    if not limits.enabled or not boundaries:
        return None
    if not force and len(boundaries) < limits.turn_interval:
        return None

    # 2. 先顺序分配事实预算，保留部分正文时显式标记，后续事件留待下批处理。
    facts: list[dict[str, Any]] = []
    used = 0
    through = 0
    for record in records:
        if record["sequence"] > boundaries[-1]:
            break
        kind = record["type"]
        if kind not in {"context_user_query_appended", "context_messages_appended", "tool_execution_finished"}:
            through = record["sequence"]
            continue
        text = json.dumps(record["payload"], ensure_ascii=False)
        allowance = min(limits.max_event_characters, limits.max_input_characters - used - 300)
        if allowance < 64:
            break
        fact = {"sequence": record["sequence"], "type": kind,
                "status": record["payload"].get("status"),
                "text": text[:allowance], "truncated": len(text) > allowance}
        used += len(json.dumps(fact, ensure_ascii=False))
        facts.append(fact)
        through = record["sequence"]
    if not facts:
        return None

    # 3. 已有记录只作为去重与替代背景，按近期证据顺序在剩余预算内携带。
    selected = []
    for record in sorted(existing, key=lambda item: max(item.source_sequences), reverse=True):
        size = len(json.dumps(asdict(record), ensure_ascii=False)) + 2
        if len(selected) >= limits.max_records or used + size + 100 > limits.max_input_characters:
            continue
        selected.append(record)
        used += size
    return ExtractionBatch(tuple(facts), through, tuple(selected))


def parse_candidates(text: str, batch: ExtractionBatch, limits: ContextExtractionLimits) -> list[ContextRecordInput]:
    """校验候选协议、证据范围和逐字来源，再返回待提交记录。"""
    payload = json.loads(text)
    if not isinstance(payload, dict) or set(payload) != {"records"}:
        raise ValueError("提取结果必须是只包含 records 的 JSON 对象")
    rows = payload["records"]
    if not isinstance(rows, list) or len(rows) > limits.max_records:
        raise ValueError("提取候选数量超出限制或不是数组")
    facts = {fact["sequence"]: fact for fact in batch.facts}
    allowed_ids = {record.record_id for record in batch.existing}
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "content", "category", "source_sequences", "supersedes", "evidence_quote"
        }:
            raise ValueError("提取候选字段不符合协议")
        content, category = row["content"], row["category"]
        sources, supersedes, quote = row["source_sequences"], row["supersedes"], row["evidence_quote"]
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 500:
            raise ValueError("提取候选正文必须是 1 到 500 字符")
        if category not in {"constraint", "fact", "decision"}:
            raise ValueError("提取候选类别无效")
        if not isinstance(sources, list) or not sources or any(
            type(sequence) is not int or sequence not in facts for sequence in sources
        ):
            raise ValueError("提取候选引用了未展示的事实")
        if not isinstance(supersedes, list) or any(
            not isinstance(record_id, str) or record_id not in allowed_ids for record_id in supersedes
        ):
            raise ValueError("提取候选替代了未提供的记录")
        user_sources = [facts[sequence] for sequence in sources
                        if facts[sequence]["type"] == "context_user_query_appended"]
        evidence = user_sources if category in {"constraint", "decision"} or supersedes else [
            facts[s] for s in sources if facts[s]["type"] == "context_user_query_appended"
            or (facts[s]["type"] == "tool_execution_finished" and facts[s]["status"] == "success")
        ]
        if not isinstance(quote, str) or not quote.strip() or not any(quote in fact["text"] for fact in evidence):
            raise ValueError("提取候选缺少可核对的来源原文")
        candidates.append(ContextRecordInput(content, category, tuple(sources), tuple(supersedes)))
    return candidates
