"""跨模型轮次复用的活动模型上下文。"""

from dataclasses import dataclass, field
from typing import Any


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
class ContextManager:
    """只保存不可变 Query、轮次边界和可压缩活动上下文。"""

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

    # 下一条用户 Query 使用的活动上下文内递增序号。
    _next_query_sequence: int = field(default=1, repr=False)

    @classmethod
    def from_messages(cls, messages: list[Any]) -> "ContextManager":
        """兼容现有列表调用方，并继续共享其活动消息列表。"""
        context_manager = cls(active_messages=messages)
        context_manager._next_query_sequence += sum(
            1 for message in messages if _is_user_message(message)
        )
        return context_manager

    @classmethod
    def from_checkpoint(cls, snapshot: dict[str, Any]) -> "ContextManager":
        """从 Checkpoint 快照恢复活动上下文。"""
        # 1. 恢复带标记的 UserQuery，避免快照把不可变 Query 降级为普通消息。
        raw_messages = snapshot.get("active_messages")
        if not isinstance(raw_messages, list):
            raise ValueError("Checkpoint 缺少活动消息")
        messages = []
        for message in raw_messages:
            if isinstance(message, dict) and message.get("type") == "user_query":
                messages.append(
                    UserQuery(message["sequence"], message["content"])
                )
            else:
                messages.append(message)

        # 2. 恢复上下文计数和轮次边界，下一条 Query 序号必须单调延续。
        manager = cls(
            active_messages=messages,
            round_boundaries=list(snapshot.get("round_boundaries", [])),
            compaction_count=int(snapshot.get("compaction_count", 0)),
            consecutive_compaction_failures=int(
                snapshot.get("consecutive_compaction_failures", 0)
            ),
            partial_compaction_count=int(snapshot.get("partial_compaction_count", 0)),
        )
        manager._next_query_sequence = int(snapshot.get("next_query_sequence", 1))
        return manager


    @property
    def user_queries(self) -> tuple[UserQuery, ...]:
        """返回当前活动上下文中无法原地修改的 Query 快照。"""
        return tuple(
            item
            for item in self.active_messages
            if isinstance(item, UserQuery)
        )

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
        """原样追加一条只属于活动模型上下文的用户 Query。"""
        query = UserQuery(self._next_query_sequence, content)
        self._next_query_sequence += 1
        self.active_messages.append(query)
        self.partial_compaction_count = 0
        return query

    def restore_user_query(self, sequence: int, content: str) -> UserQuery:
        """按 Transcript 中的稳定序号恢复一条用户 Query。"""
        # 1. 恢复协议只接受严格递增的 Query，避免损坏日志产生重复身份。
        if sequence != self._next_query_sequence:
            raise ValueError(
                f"用户 Query 序号不连续：期望 {self._next_query_sequence}，收到 {sequence}"
            )

        # 2. 恢复与正常追加使用相同的活动消息结构，但不重新分配序号。
        query = UserQuery(sequence, content)
        self._next_query_sequence = sequence + 1
        self.active_messages.append(query)
        self.partial_compaction_count = 0
        return query

    def append_message(self, message: Any) -> None:
        """追加普通消息；user 消息自动进入不可变 Query 管线。"""
        if _is_user_message(message):
            self.append_user_query(message["content"])
            return
        self.active_messages.append(message)
        self.partial_compaction_count = 0

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
    ) -> dict[str, Any]:
        """提交已经验证收益的候选压缩，并返回结构化边界事实。"""
        compacted_messages, compacted_boundaries, preserved_queries = (
            self.build_compacted_messages(cut_index, summary)
        )
        self.active_messages[:] = compacted_messages
        self.round_boundaries[:] = compacted_boundaries
        boundary_record = {
            "summary": summary,
            "compacted_messages": cut_index,
            "preserved_query_sequences": [query.sequence for query in preserved_queries],
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "summary_tokens": summary_tokens,
            "recent_tokens": recent_tokens,
            "status": status,
        }
        self.compaction_count += 1
        self.consecutive_compaction_failures = 0
        if status == "partial":
            self.partial_compaction_count += 1
        else:
            self.partial_compaction_count = 0
        # 恢复端直接采用已经提交的结果，不重新推导历史轮次边界。
        boundary_record["resulting_round_boundaries"] = list(self.round_boundaries)
        boundary_record["resulting_message_count"] = len(self.active_messages)
        boundary_record["compaction_count"] = self.compaction_count
        boundary_record["partial_compaction_count"] = self.partial_compaction_count
        return boundary_record

    def restore_compaction(self, record: dict[str, Any]) -> None:
        """重放一次已经成功提交的上下文压缩。"""
        # 1. 先按当时的切点和摘要重建候选消息，校验日志没有越界。
        cut_index = record.get("compacted_messages")
        summary = record.get("summary")
        if not isinstance(cut_index, int) or not 0 < cut_index <= len(
            self.active_messages
        ):
            raise ValueError("上下文压缩记录包含无效切点")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("上下文压缩记录缺少有效摘要")
        compacted_messages, _, _ = self.build_compacted_messages(
            cut_index,
            summary,
        )

        # 2. 轮次边界属于压缩提交结果，恢复时必须直接采用持久化值。
        boundaries = record.get("resulting_round_boundaries")
        if not isinstance(boundaries, list) or not all(
            isinstance(boundary, int) for boundary in boundaries
        ):
            raise ValueError("上下文压缩记录缺少提交后的轮次边界")
        if boundaries != sorted(set(boundaries)) or any(
            boundary <= 0 or boundary > len(compacted_messages)
            for boundary in boundaries
        ):
            raise ValueError("上下文压缩记录的轮次边界无效")
        if record.get("resulting_message_count") != len(compacted_messages):
            raise ValueError("上下文压缩记录的消息数量与重放结果不一致")

        # 3. 原子替换活动上下文并恢复压缩熔断相关计数。
        self.active_messages[:] = compacted_messages
        self.round_boundaries[:] = boundaries
        self.compaction_count = int(record.get("compaction_count", 0))
        self.partial_compaction_count = int(
            record.get("partial_compaction_count", 0)
        )
        self.consecutive_compaction_failures = 0

    def record_compaction_failure(self) -> None:
        """累计一次压缩失败，供后续熔断判断使用。"""
        self.consecutive_compaction_failures += 1

def _is_user_message(message: Any) -> bool:
    """判断一条消息是否为包含原始文本的用户 Query。"""
    return (
        isinstance(message, dict)
        and message.get("role") == "user"
        and isinstance(message.get("content"), str)
    )
