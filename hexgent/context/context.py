"""跨模型轮次和用户轮次复用的会话上下文状态。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .session_store import SessionStore, ToolResultLimits


@dataclass(frozen=True)
class UserQuery:
    """一条内容不可修改、只能追加保存的用户原始请求。"""

    # 当前 Query 在本次会话中的稳定递增序号。
    sequence: int

    # 用户提交的原始文本，不允许摘要、改写或截断。
    content: str

    def to_message(self) -> dict[str, str]:
        """转换为提供给模型的原始 user 消息。"""
        return {"role": "user", "content": self.content}


@dataclass
class ConversationContext:
    """分别保存不可变 Query、完整记录和可压缩活动上下文。"""

    # 不因上下文压缩而删除的完整内存会话记录。
    transcript: list[Any] = field(default_factory=list)

    # 下一次模型调用使用的活动条目，其中 UserQuery 永远不可被压缩。
    active_messages: list[Any] = field(default_factory=list)

    # 活动消息中完整 API 轮次结束位置组成的排他索引。
    round_boundaries: list[int] = field(default_factory=list)

    # 当前会话已经成功完成的上下文压缩次数。
    compaction_count: int = 0

    # 最近连续发生的上下文压缩失败次数。
    consecutive_compaction_failures: int = 0

    # 当前连续压缩流程已经接受的部分有效压缩次数。
    partial_compaction_count: int = 0

    # 可选的会话持久化实现；为空时仅保留内存记录。
    session_store: SessionStore | None = None

    # 只能通过 append_user_query() 追加的不可变用户 Query 集合。
    _user_queries: tuple[UserQuery, ...] = field(default_factory=tuple, repr=False)

    @classmethod
    def persistent(
        cls,
        workdir: Path,
        tool_result_limits: ToolResultLimits | None = None,
    ) -> "ConversationContext":
        """创建一个在工作区中持续写入 JSONL Transcript 的上下文。"""
        return cls(
            session_store=SessionStore(
                workdir,
                tool_result_limits=tool_result_limits,
            )
        )

    @classmethod
    def from_messages(cls, messages: list[Any]) -> "ConversationContext":
        """兼容现有列表调用方，并提取其中不可变的用户 Query。"""
        conversation = cls(transcript=messages)
        for message in messages:
            if _is_user_message(message):
                conversation._append_user_query(message["content"], persist=False)
            else:
                conversation.active_messages.append(message)
        return conversation

    @property
    def user_queries(self) -> tuple[UserQuery, ...]:
        """返回无法由调用方原地修改的用户 Query 快照。"""
        return self._user_queries

    def model_messages(self) -> list[Any]:
        """把不可变 Query 物化为原始 user 消息后构造模型输入。"""
        return self.materialize_messages(self.active_messages)

    def materialize_messages(self, messages: list[Any]) -> list[Any]:
        """将任意活动条目列表中的不可变 Query 转换为模型消息。"""
        return [
            item.to_message() if isinstance(item, UserQuery) else item
            for item in messages
        ]

    def append_user_query(self, content: str) -> UserQuery:
        """原样追加一条用户 Query，并同时记录到 Transcript。"""
        return self._append_user_query(content, persist=True)

    def _append_user_query(self, content: str, *, persist: bool) -> UserQuery:
        """创建不可变 Query；兼容初始化时可跳过重复 Transcript 写入。"""
        query = UserQuery(len(self._user_queries) + 1, content)
        self._user_queries = (*self._user_queries, query)
        self.active_messages.append(query)
        self.partial_compaction_count = 0
        if persist:
            message = query.to_message()
            self.transcript.append(message)
            if self.session_store is not None:
                self.session_store.start_session(content)
            self._persist(
                "user_query",
                {"sequence": query.sequence, "content": content},
            )
        return query

    def append_message(self, message: Any) -> None:
        """追加普通消息；user 消息自动进入不可变 Query 管线。"""
        if _is_user_message(message):
            self.append_user_query(message["content"])
            return
        self.transcript.append(message)
        self.active_messages.append(message)
        self.partial_compaction_count = 0
        self._persist("message", message)

    def append_messages(
        self,
        messages: list[Any],
        *,
        completes_round: bool = False,
    ) -> None:
        """同时追加多条消息，并可标记一个完整 API 轮次结束。"""
        for message in messages:
            self.append_message(message)
        if completes_round:
            self.mark_round_complete()

    def mark_round_complete(self) -> None:
        """记录当前活动消息末尾是一个可安全压缩的完整轮次边界。"""
        boundary = len(self.active_messages)
        if boundary > 0 and (
            not self.round_boundaries or self.round_boundaries[-1] != boundary
        ):
            self.round_boundaries.append(boundary)

    def build_compacted_messages(
        self,
        cut_index: int,
        summary: str,
    ) -> tuple[list[Any], list[int], list[UserQuery]]:
        """构造候选活动上下文，但不立即修改当前会话状态。"""
        latest_query = next(
            (
                item
                for item in reversed(self.active_messages)
                if isinstance(item, UserQuery)
            ),
            None,
        )
        preserved_queries = (
            [latest_query]
            if latest_query is not None
            and latest_query in self.active_messages[:cut_index]
            else []
        )
        summary_message = {
            "role": "developer",
            "content": f"以下是此前会话的压缩摘要：\n\n{summary}",
        }
        remaining_messages = self.active_messages[cut_index:]
        prefix_length = len(preserved_queries) + 1
        remaining_boundaries = [
            prefix_length + boundary - cut_index
            for boundary in self.round_boundaries
            if boundary > cut_index
        ]

        compacted_messages = [
            *preserved_queries,
            summary_message,
            *remaining_messages,
        ]
        compacted_boundaries = [prefix_length, *remaining_boundaries]
        return compacted_messages, compacted_boundaries, preserved_queries

    def apply_compaction(
        self,
        cut_index: int,
        summary: str,
        *,
        before_tokens: int,
        after_tokens: int,
        summary_tokens: int,
        recent_tokens: int,
        status: str,
    ) -> None:
        """提交已经验证收益的候选压缩，并记录完整观测指标。"""
        compacted_messages, compacted_boundaries, preserved_queries = (
            self.build_compacted_messages(cut_index, summary)
        )
        self.active_messages[:] = compacted_messages
        self.round_boundaries[:] = compacted_boundaries
        boundary_record = {
            "type": "context_compaction_boundary",
            "summary": summary,
            "compacted_messages": cut_index,
            "preserved_query_sequences": [query.sequence for query in preserved_queries],
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "summary_tokens": summary_tokens,
            "recent_tokens": recent_tokens,
            "status": status,
        }
        self.transcript.append(boundary_record)
        self._persist("context_compaction_boundary", boundary_record)
        self.compaction_count += 1
        self.consecutive_compaction_failures = 0
        if status == "partial":
            self.partial_compaction_count += 1
        else:
            self.partial_compaction_count = 0

    def record_compaction_failure(self) -> None:
        """累计一次压缩失败，供后续熔断判断使用。"""
        self.consecutive_compaction_failures += 1

    def _persist(self, record_type: str, payload: Any) -> None:
        """在启用持久化时追加一条 Transcript 记录。"""
        if self.session_store is not None:
            self.session_store.append_transcript(record_type, payload)

    def record_runtime_event(self, event_type: str, payload: Any) -> None:
        """记录不进入模型上下文、但属于完整会话过程的运行时事件。"""
        self._persist(event_type, payload)


def _is_user_message(message: Any) -> bool:
    """判断一条消息是否为包含原始文本的用户 Query。"""
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    )
