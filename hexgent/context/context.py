"""跨模型轮次和用户轮次复用的会话上下文状态。"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ConversationContext:
    """分别保存完整会话记录和当前模型使用的活动上下文。"""

    # 不因上下文压缩而删除的完整会话记录。
    transcript: list[Any] = field(default_factory=list)

    # 下一次模型调用实际使用、允许被压缩替换的消息列表。
    active_messages: list[Any] = field(default_factory=list)

    # 活动消息中完整 API 轮次结束位置组成的排他索引。
    round_boundaries: list[int] = field(default_factory=list)

    # 当前会话已经成功完成的上下文压缩次数。
    compaction_count: int = 0

    # 最近连续发生的上下文压缩失败次数。
    consecutive_compaction_failures: int = 0

    @classmethod
    def from_messages(cls, messages: list[Any]) -> "ConversationContext":
        """兼容现有列表调用方，并将原列表作为完整会话记录。"""
        return cls(transcript=messages, active_messages=list(messages))

    def append_message(self, message: Any) -> None:
        """向完整记录和活动上下文同时追加一条消息。"""
        self.transcript.append(message)
        self.active_messages.append(message)

    def append_messages(
        self,
        messages: list[Any],
        *,
        completes_round: bool = False,
    ) -> None:
        """同时追加多条消息，并可标记一个完整 API 轮次结束。"""
        self.transcript.extend(messages)
        self.active_messages.extend(messages)
        if completes_round:
            self.mark_round_complete()

    def mark_round_complete(self) -> None:
        """记录当前活动消息末尾是一个可安全压缩的完整轮次边界。"""
        boundary = len(self.active_messages)
        if boundary > 0 and (
            not self.round_boundaries or self.round_boundaries[-1] != boundary
        ):
            self.round_boundaries.append(boundary)

    def apply_compaction(self, cut_index: int, summary: str) -> None:
        """用摘要替换指定边界之前的活动消息，并保留完整记录。"""
        summary_message = {
            "role": "developer",
            "content": f"以下是此前会话的压缩摘要：\n\n{summary}",
        }
        remaining_messages = self.active_messages[cut_index:]
        remaining_boundaries = [
            1 + boundary - cut_index
            for boundary in self.round_boundaries
            if boundary > cut_index
        ]

        self.active_messages[:] = [summary_message, *remaining_messages]
        self.round_boundaries[:] = [1, *remaining_boundaries]
        self.transcript.append(
            {
                "type": "context_compaction_boundary",
                "summary": summary,
                "compacted_messages": cut_index,
            }
        )
        self.compaction_count += 1
        self.consecutive_compaction_failures = 0

    def record_compaction_failure(self) -> None:
        """累计一次压缩失败，供后续熔断判断使用。"""
        self.consecutive_compaction_failures += 1
