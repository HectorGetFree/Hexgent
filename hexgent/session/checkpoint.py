"""会话安全边界 Checkpoint 的序列化与原子持久化。"""

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Any

from ..context import ContextManager, UserQuery
from ..plan import Plan


CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Checkpoint:
    """一个完整 Turn 边界上的可恢复状态快照。"""

    # 快照协议版本，用于拒绝不兼容的历史快照。
    schema_version: int
    # 所属 Session 的稳定机器标识。
    session_id: str
    # 快照覆盖到的 Transcript 最后一条序号。
    transcript_sequence: int
    # Session 内递增的快照标识。
    checkpoint_id: str
    # 创建快照的原因，例如 turn_completed。
    reason: str
    # 活动 Context 的可序列化状态。
    context: dict[str, Any]
    # 当前 Plan 完整快照，没有 Plan 时为 null。
    plan: dict[str, Any] | None


class CheckpointStore:
    """负责 Checkpoint 文件的原子写入和严格读取。"""

    def __init__(self, session_dir: Path) -> None:
        self.directory = session_dir / "checkpoints"

    def write(
        self,
        *,
        session_id: str,
        transcript_sequence: int,
        context_manager: ContextManager,
        plan: Plan | None,
        reason: str,
    ) -> Checkpoint:
        """在安全边界原子写入一个 Checkpoint。"""
        # 1. 使用已经提交的内存状态构造不可变快照，不包含线程和网络对象。
        checkpoint_id = self._next_checkpoint_id()
        checkpoint = Checkpoint(
            CHECKPOINT_SCHEMA_VERSION,
            session_id,
            transcript_sequence,
            checkpoint_id,
            reason,
            _context_snapshot(context_manager),
            asdict(plan) if plan is not None else None,
        )

        # 2. 先写临时文件再原子替换，进程中断不会留下半个 JSON。
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{checkpoint_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(checkpoint), ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)
        return checkpoint

    def read(self, checkpoint_id: str) -> Checkpoint:
        """严格读取并校验指定 Checkpoint。"""
        path = self.directory / f"{checkpoint_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError("Checkpoint 协议版本不兼容")
        if payload.get("checkpoint_id") != checkpoint_id:
            raise ValueError("Checkpoint 标识不一致")
        return Checkpoint(**payload)

    def list_ids(self) -> list[str]:
        """按新到旧返回可读取的 Checkpoint ID。"""
        if not self.directory.is_dir():
            return []
        return sorted(
            (path.stem for path in self.directory.glob("cp-*.json")),
            reverse=True,
        )

    def _next_checkpoint_id(self) -> str:
        """分配只包含数字、字母和短横线的快照 ID。"""
        numbers = []
        if self.directory.is_dir():
            for path in self.directory.glob("cp-*.json"):
                try:
                    numbers.append(int(path.stem.removeprefix("cp-")))
                except ValueError:
                    continue
        return f"cp-{max(numbers, default=0) + 1:06d}"


def _context_snapshot(context_manager: ContextManager) -> dict[str, Any]:
    """把 UserQuery 转换为带标记的 JSON 消息，保留其不可变语义。"""
    return {
        "active_messages": [
            {
                "type": "user_query",
                "sequence": message.sequence,
                "content": message.content,
            }
            if isinstance(message, UserQuery)
            else message
            for message in context_manager.active_messages
        ],
        "round_boundaries": list(context_manager.round_boundaries),
        "compaction_count": context_manager.compaction_count,
        "consecutive_compaction_failures": context_manager.consecutive_compaction_failures,
        "partial_compaction_count": context_manager.partial_compaction_count,
        "next_query_sequence": context_manager._next_query_sequence,
    }
