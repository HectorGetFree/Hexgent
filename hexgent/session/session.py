"""管理 Session 生命周期、Transcript 和会话工件。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..context.context_manager import UserQuery
from .session_store import SessionStore, ToolResultLimits


@dataclass
class Session:
    """独立于模型 Context 的可持久化会话能力。"""

    # 可选的 JSONL Transcript 与会话工件存储。
    store: SessionStore | None = None

    @classmethod
    def persistent(
        cls,
        workdir: Path,
        tool_result_limits: ToolResultLimits | None = None,
    ) -> "Session":
        """创建持续写入工作区 Session 目录的会话。"""
        return cls(SessionStore(workdir, tool_result_limits=tool_result_limits))

    def record_user_query(self, query: UserQuery) -> None:
        """先初始化 Session，再原样记录已被 Context 接受的 Query。"""
        if self.store is not None:
            self.store.start_session(query.content)
            self.store.append_transcript(
                "user_query",
                {"sequence": query.sequence, "content": query.content},
            )

    def record_message(self, message: Any) -> None:
        """记录一条已经进入活动 Context 的普通消息。"""
        self.record_runtime_event("message", message)

    def record_messages(self, messages: list[Any]) -> None:
        """按顺序记录多条已经进入活动 Context 的消息。"""
        for message in messages:
            self.record_message(message)

    def record_runtime_event(self, event_type: str, payload: Any) -> None:
        """记录不进入模型 Context 的完整 Session 事实。"""
        if self.store is not None:
            self.store.append_transcript(event_type, payload)

    def record_compaction_boundary(self, boundary: dict[str, Any]) -> None:
        """把 Context 已经提交的压缩边界追加到 Transcript。"""
        self.record_runtime_event("context_compaction_boundary", boundary)
