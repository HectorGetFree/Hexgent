"""Memory 领域中 Session 范围的核心上下文记录。"""

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any, Iterable, Literal


ContextRecordCategory = Literal[
    "constraint",  # 用户要求、偏好和不可违反的边界。
    "fact",  # 可由 Transcript、工具结果或文件内容佐证的事实。
    "decision",  # 已确认的设计选择与取舍结论。
]
ContextRecordStatus = Literal[
    "active",  # 当前仍然有效，后续检索阶段可以召回。
    "superseded",  # 已被新记录替代，仅保留审计与追溯价值。
]

VALID_CONTEXT_RECORD_CATEGORIES = {"constraint", "fact", "decision"}
VALID_CONTEXT_RECORD_STATUSES = {"active", "superseded"}


@dataclass(frozen=True)
class ContextRecordInput:
    """上游向 Session 提交的候选上下文记录。"""

    # 仅保留可验证、可独立理解的简短事实或约束。
    content: str

    # 记录语义类别，决定后续的检索和注入规则。
    category: ContextRecordCategory

    # 产生该记录的 Transcript 事实序号，至少需要一个证据来源。
    source_sequences: tuple[int, ...]

    # 被本记录明确替代的旧记录 ID；不进行隐式语义替代。
    supersedes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextRecord:
    """当前 Session 中一条可追溯的核心上下文记录。"""

    # 稳定机器标识，由类别和规范化内容生成。
    record_id: str

    # 面向模型和用户的简短事实、约束或决策文本。
    content: str

    # constraint / fact / decision 三种最小语义分类。
    category: ContextRecordCategory

    # 支撑该记录的全部 Transcript 证据序号。
    source_sequences: tuple[int, ...]

    # active 表示仍有效；superseded 表示已经被新条目覆盖。
    status: ContextRecordStatus = "active"


ContextRecordMutationAction = Literal[
    "created",  # 本次首次创建新记录。
    "updated",  # 与已有记录合并了新的证据来源。
    "superseded",  # 已有记录被明确标记为失效。
]


@dataclass(frozen=True)
class ContextRecordMutation:
    """一次 upsert 产生的单条可持久化状态变更。"""

    # created / updated / superseded，供 Transcript 和投影审计使用。
    action: ContextRecordMutationAction

    # 变更后的完整记录快照。
    record: ContextRecord


class ContextRecordStore:
    """维护 Session 内记录的新增、合并、替代与重放。"""

    def __init__(self, records: Iterable[ContextRecord] = ()) -> None:
        self._records: dict[str, ContextRecord] = {}
        for record in records:
            self._put_restored(record)

    @property
    def records(self) -> tuple[ContextRecord, ...]:
        """按稳定 ID 返回完整记录投影。"""
        return tuple(
            self._records[record_id] for record_id in sorted(self._records)
        )

    @property
    def active_records(self) -> tuple[ContextRecord, ...]:
        """返回检索阶段可见的有效记录。"""
        return tuple(
            record for record in self.records if record.status == "active"
        )

    def upsert(self, candidates: Iterable[ContextRecordInput]) -> tuple[ContextRecordMutation, ...]:
        """显式创建、合并或替代记录，不进行不可解释的语义猜测。"""
        mutations: list[ContextRecordMutation] = []
        for candidate in candidates:
            normalized = _normalize_input(candidate)
            mutations.extend(self._supersede(normalized.supersedes))
            record_id = _build_record_id(normalized.category, normalized.content)
            existing = self._records.get(record_id)
            if existing is None:
                record = ContextRecord(
                    record_id=record_id,
                    content=normalized.content,
                    category=normalized.category,
                    source_sequences=normalized.source_sequences,
                )
                self._records[record_id] = record
                mutations.append(ContextRecordMutation("created", record))
                continue

            merged = replace(
                existing,
                source_sequences=tuple(
                    sorted(set(existing.source_sequences) | set(normalized.source_sequences))
                ),
                status="active",
            )
            if merged != existing:
                self._records[record_id] = merged
                mutations.append(ContextRecordMutation("updated", merged))
        return tuple(mutations)

    def apply_mutation(self, mutation: ContextRecordMutation) -> None:
        """从 Transcript 重放一条已经验证的状态变更。"""
        record = _normalize_record(mutation.record)
        if mutation.action not in {"created", "updated", "superseded"}:
            raise ValueError(f"ContextRecord 变更动作无效：{mutation.action}")
        self._records[record.record_id] = record

    def to_dict(self) -> dict[str, Any]:
        """构造可删除、可由 Transcript 重建的 JSON 投影。"""
        return {
            "schema_version": 2,
            "records": [asdict(record) for record in self.records],
        }

    @classmethod
    def mutation_from_payload(cls, payload: dict[str, Any]) -> ContextRecordMutation:
        """严格解析 Transcript 中的记录变更事实。"""
        action = payload.get("action")
        raw_record = payload.get("record", payload.get("item"))
        if action not in {"created", "updated", "superseded"}:
            raise ValueError("ContextRecord 变更缺少有效 action")
        if not isinstance(raw_record, dict):
            raise ValueError("ContextRecord 变更缺少 record 对象")
        record = _record_from_payload(raw_record)
        return ContextRecordMutation(action, record)

    def _supersede(self, record_ids: tuple[str, ...]) -> list[ContextRecordMutation]:
        """仅替代已存在且有效的记录，重复替代是幂等操作。"""
        mutations: list[ContextRecordMutation] = []
        for record_id in record_ids:
            existing = self._records.get(record_id)
            if existing is None:
                raise ValueError(f"待替代的 ContextRecord 不存在：{record_id}")
            if existing.status == "superseded":
                continue
            superseded = replace(existing, status="superseded")
            self._records[record_id] = superseded
            mutations.append(ContextRecordMutation("superseded", superseded))
        return mutations

    def _put_restored(self, record: ContextRecord) -> None:
        """初始化时严格校验投影，避免接受不可信内存状态。"""
        normalized = _normalize_record(record)
        if normalized.record_id in self._records:
            raise ValueError(f"ContextRecord 标识重复：{normalized.record_id}")
        self._records[normalized.record_id] = normalized


def _normalize_input(candidate: ContextRecordInput) -> ContextRecordInput:
    """规范化上游候选值并校验证据边界。"""
    content = " ".join(candidate.content.split())
    if not content or len(content) > 500:
        raise ValueError("ContextRecord content 必须是 1 到 500 字符的非空文本")
    if candidate.category not in VALID_CONTEXT_RECORD_CATEGORIES:
        raise ValueError(f"ContextRecord category 无效：{candidate.category}")
    source_sequences = _normalize_sequences(candidate.source_sequences)
    supersedes = tuple(dict.fromkeys(candidate.supersedes))
    if any(not record_id.startswith("ctx-") for record_id in supersedes):
        raise ValueError("ContextRecord supersedes 包含无效标识")
    return ContextRecordInput(
        content,
        candidate.category,
        source_sequences,
        supersedes,
    )


def _normalize_record(record: ContextRecord) -> ContextRecord:
    """校验持久化记录与其稳定内容标识完全一致。"""
    candidate = _normalize_input(
        ContextRecordInput(
            record.content,
            record.category,
            record.source_sequences,
        )
    )
    expected_id = _build_record_id(candidate.category, candidate.content)
    if record.record_id != expected_id:
        raise ValueError("ContextRecord 标识与内容不一致")
    if record.status not in VALID_CONTEXT_RECORD_STATUSES:
        raise ValueError(f"ContextRecord status 无效：{record.status}")
    return ContextRecord(
        expected_id,
        candidate.content,
        candidate.category,
        candidate.source_sequences,
        record.status,
    )


def _record_from_payload(payload: dict[str, Any]) -> ContextRecord:
    """从 JSON 对象读取 ContextRecord。"""
    try:
        raw_sequences = payload["source_sequences"]
        if not isinstance(raw_sequences, list):
            raise ValueError("ContextRecord source_sequences 必须是数组")
        source_sequences = tuple(raw_sequences)
        record_id = payload.get("record_id", payload.get("item_id"))
        return _normalize_record(
            ContextRecord(
                record_id,
                payload["content"],
                payload["category"],
                source_sequences,
                payload.get("status", "active"),
            )
        )
    except (KeyError, TypeError) as error:
        raise ValueError("ContextRecord 持久化结构无效") from error


def _normalize_sequences(values: tuple[int, ...]) -> tuple[int, ...]:
    """确保每条记录至少关联一条有效 Transcript 证据。"""
    if not values or any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1
        for value in values
    ):
        raise ValueError("ContextRecord source_sequences 必须包含正整数")
    return tuple(sorted(set(values)))


def _build_record_id(category: ContextRecordCategory, content: str) -> str:
    """由类别和规范化内容生成稳定且可读的机器标识。"""
    digest = sha256(f"{category}\n{content}".encode("utf-8")).hexdigest()[:12]
    return f"ctx-{digest}"
