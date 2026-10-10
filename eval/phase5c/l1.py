"""L1 Stage A qualification 与完整 workspace snapshot 克隆。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hexgent.memory import ContextRecord

from .fixtures import freeze_snapshot


@dataclass(frozen=True)
class Qualification:
    qualified: bool
    reason: str
    record_id: str | None = None


def qualify_stage_a_record(
    record: ContextRecord,
    transcript: list[dict[str, Any]],
    *,
    stage_b_prompt: str,
    final_answer_markers: tuple[str, ...],
) -> Qualification:
    """只接受 active、有来源、相关且不泄露 Stage B 答案的 durable Record。"""

    if record.status != "active":
        return Qualification(False, "STAGE_A_RECORD_NOT_QUALIFIED: record_not_active")
    by_sequence = {item.get("sequence"): item for item in transcript}
    sources = [by_sequence.get(sequence) for sequence in record.source_sequences]
    if not sources or any(source is None for source in sources):
        return Qualification(False, "STAGE_A_RECORD_NOT_QUALIFIED: missing_provenance")
    source_text = "\n".join(str(source.get("payload", "")) for source in sources if source)
    if not any(token in source_text for token in _tokens(record.content)):
        return Qualification(False, "STAGE_A_RECORD_NOT_QUALIFIED: record_not_grounded_in_source")
    shared = set(_tokens(record.content)) & set(_tokens(stage_b_prompt))
    if not shared:
        return Qualification(False, "STAGE_A_RECORD_NOT_QUALIFIED: not_relevant_to_stage_b")
    if any(marker and marker in record.content for marker in final_answer_markers):
        return Qualification(False, "STAGE_A_RECORD_NOT_QUALIFIED: contains_stage_b_answer")
    return Qualification(True, "QUALIFIED", record.record_id)


def freeze_qualified_stage_a(workspace: Path, destination: Path, qualification: Qualification) -> Path:
    if not qualification.qualified:
        raise ValueError(qualification.reason)
    return freeze_snapshot(workspace, destination)


def _tokens(value: str) -> tuple[str, ...]:
    normalized = "".join(character.lower() if character.isalnum() else " " for character in value)
    words = [token for token in normalized.split() if len(token) >= 2]
    chinese = "".join(character for character in normalized if "\u4e00" <= character <= "\u9fff")
    bigrams = [chinese[index : index + 2] for index in range(max(0, len(chinese) - 1))]
    return tuple(words + bigrams)
