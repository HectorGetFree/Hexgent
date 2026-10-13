"""会话安全边界 Checkpoint 的序列化与原子持久化。"""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Literal

from ..context import ContextManager, UserQuery
from ..plan import Plan
from ..loop.loop_state import LoopState, RunStateSnapshot


CHECKPOINT_SCHEMA_VERSION = 2

CheckpointReason = Literal[
    "tool_batch_completed",  # 工具批次结果已经完整回填。
    "plan_continuation_ready",  # Plan 继续提醒已经写入 Context。
    "context_compaction_completed",  # 主动上下文压缩已经提交。
    "reactive_compaction_completed",  # 应急上下文压缩已经提交。
    "finalization_ready",  # 预算收尾状态已经写入 Context。
    "steering_applied",  # Steering Query 已在安全 Turn 边界进入 Context。
    "run_finished",  # 当前 Agent Run 已经终止。
]

CheckpointRunStatus = Literal[
    "active",  # Run 在安全边界后仍可继续，进程崩溃后允许恢复。
    "continuable",  # Run 已暂停或不完整结束，允许后续 continue。
    "terminal",  # Run 已完成、失败、取消或阻塞，不允许继续原状态。
]


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
    reason: CheckpointReason
    # 活动 Context 的可序列化状态。
    context: dict[str, Any]
    # 当前 Plan 完整快照，没有 Plan 时为 null。
    plan: dict[str, Any] | None
    # 本次 Run 下一轮需要继承的预算和控制状态；终态快照可为空。
    run_state: dict[str, Any] | None
    # 说明快照中的 Run 是否仍可通过 engine.run(continue) 执行。
    run_status: CheckpointRunStatus
    # 不含本字段的内容校验和，用于拒绝损坏或被改写的快照。
    checksum: str


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
        reason: CheckpointReason,
        loop_state: LoopState | None,
        run_status: CheckpointRunStatus,
    ) -> Checkpoint:
        """在安全边界原子写入一个 Checkpoint。"""
        # 1. 使用已经提交的内存状态构造不可变快照，不包含线程和网络对象。
        checkpoint_id = self._next_checkpoint_id()
        payload = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "session_id": session_id,
            "transcript_sequence": transcript_sequence,
            "checkpoint_id": checkpoint_id,
            "reason": reason,
            "context": _context_snapshot(context_manager),
            "plan": asdict(plan) if plan is not None else None,
            "run_state": loop_state.to_snapshot().to_dict() if loop_state else None,
            "run_status": run_status,
        }
        checkpoint = Checkpoint(**payload, checksum=_checksum(payload))

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
        checksum = payload.pop("checksum", None)
        if not isinstance(checksum, str) or checksum != _checksum(payload):
            raise ValueError("Checkpoint 校验和不匹配")
        checkpoint = Checkpoint(**payload, checksum=checksum)
        if checkpoint.run_status not in {"active", "continuable", "terminal"}:
            raise ValueError("Checkpoint 运行状态无效")
        if checkpoint.run_state is not None:
            RunStateSnapshot.from_dict(checkpoint.run_state)
        return checkpoint

    def update_index(self, checkpoint: Checkpoint) -> None:
        """原子更新可选索引；恢复不依赖该派生文件。"""
        # 1. 索引只用于快速浏览，Transcript 里的 committed 事件才是提交事实。
        self.directory.mkdir(parents=True, exist_ok=True)
        index_path = self.directory / "checkpoint-index.json"
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            payload = {"schema_version": CHECKPOINT_SCHEMA_VERSION, "checkpoints": []}
        checkpoints = [
            item
            for item in payload.get("checkpoints", [])
            if isinstance(item, dict) and item.get("checkpoint_id") != checkpoint.checkpoint_id
        ]
        checkpoints.append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "transcript_sequence": checkpoint.transcript_sequence,
                "reason": checkpoint.reason,
                "checksum": checkpoint.checksum,
            }
        )
        next_payload = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "latest_checkpoint_id": checkpoint.checkpoint_id,
            "checkpoints": checkpoints,
        }
        temporary = index_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(next_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(index_path)

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
            # SDK 的 ResponseOutputMessage、ResponseReasoningItem 等对象不能
            # 直接 JSON 序列化；Checkpoint 只保存其模型输入等价的基础数据。
            else _json_value(message)
            for message in context_manager.active_messages
        ],
        "round_boundaries": list(context_manager.round_boundaries),
        "compaction_count": context_manager.compaction_count,
        "consecutive_compaction_failures": context_manager.consecutive_compaction_failures,
        "partial_compaction_count": context_manager.partial_compaction_count,
        "next_query_sequence": context_manager._next_query_sequence,
        "message_ids": list(context_manager.message_ids),
        "next_message_sequence": context_manager.next_message_sequence,
        "key_fact_snapshot": context_manager.key_fact_snapshot,
        "source_ids_legacy": context_manager.source_ids_legacy,
    }


def _json_value(value: Any) -> Any:
    """递归转换 SDK 消息，拒绝静默降级不可恢复的未知对象。"""
    # 1. 基础 JSON 类型直接保留，保证快照与模型输入语义一致。
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    # 2. Responses SDK 使用 Pydantic 模型，优先获得 JSON 模式的完整字典。
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_value(model_dump(mode="json"))
        except TypeError:
            return _json_value(model_dump())

    # 3. 递归处理模型消息中的 content、annotations 等嵌套结构。
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]

    # 4. 未知运行时对象不允许被 str() 偷偷替换，避免恢复出错误上下文。
    raise TypeError(f"Checkpoint 不支持序列化 {type(value).__name__} 类型的上下文消息")


def _checksum(payload: dict[str, Any]) -> str:
    """计算稳定 JSON 表示的 SHA-256 校验和。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()
