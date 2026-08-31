"""Agent Run 的暂停、取消和 Steering 业务控制流。"""

from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Literal

from .interrupt import InterruptController


TerminalControlAction = Literal[
    "pause",  # 安全停止当前 Run，但保留活动 Plan 供后续 Query 使用。
    "cancel",  # 安全停止当前 Run，并终结当前活动 Plan 的执行义务。
]
UserControlStopReason = Literal[
    "user_pause",  # 用户暂停当前 Run，状态仍可在后续请求中恢复。
    "user_cancel",  # 用户取消当前任务，不再继续旧 Plan。
]


@dataclass(frozen=True)
class SteeringQuery:
    """一条等待在完整 Turn 边界注入 Context 的原始用户 Query。"""

    # 当前 Run 内按提交顺序生成的稳定递增编号。
    sequence: int

    # 用户提交的原始文本，不允许合并、摘要或改写。
    content: str


class SteeringMailbox:
    """在线程之间按 FIFO 保存当前 Run 的 Steering Query。"""

    def __init__(self) -> None:
        # 尚未被 Agent Turn 应用的 Query 队列。
        self._queries: deque[SteeringQuery] = deque()

        # 下一条 Steering Query 使用的当前 Run 内递增编号。
        self._next_sequence = 1

        # 保护交互线程提交与 Agent 线程批量取出之间的顺序一致性。
        self._lock = Lock()

    def submit(self, content: str) -> SteeringQuery:
        """把非空原始 Query 追加到 FIFO 尾部。"""
        if not content.strip():
            raise ValueError("Steering Query 不能为空")
        with self._lock:
            query = SteeringQuery(self._next_sequence, content)
            self._next_sequence += 1
            self._queries.append(query)
            return query

    def drain(self) -> list[SteeringQuery]:
        """原子取出当前已经到达的全部 Query，并保持提交顺序。"""
        with self._lock:
            queries = list(self._queries)
            self._queries.clear()
            return queries

    @property
    def has_pending(self) -> bool:
        """返回是否存在等待注入的 Steering Query。"""
        with self._lock:
            return bool(self._queries)


class RunControl:
    """把业务控制意图与一次性底层中断机制组合为 Run 控制入口。"""

    def __init__(
        self,
        interrupt_controller: InterruptController | None = None,
    ) -> None:
        # Pause 和 Cancel 用于唤醒当前模型流并阻止后续工具启动。
        self.interrupt_controller = interrupt_controller or InterruptController()

        # Steering 只在完整 Turn 边界应用，第一版不会主动打断当前阶段。
        self.steering_mailbox = SteeringMailbox()

        # 当前 Run 已请求的终止动作；Cancel 可以覆盖先到达的 Pause。
        self._terminal_action: TerminalControlAction | None = None

        # 保护交互线程与 Agent 线程之间的终止动作读写。
        self._lock = Lock()

    def pause(self) -> None:
        """请求当前 Run 安全停止并保留活动 Plan。"""
        with self._lock:
            if self._terminal_action is None:
                self._terminal_action = "pause"
        self.interrupt_controller.interrupt()

    def pause_from_signal(self) -> None:
        """从终端信号登记 Pause，并异步唤醒模型流等阻塞资源。"""
        with self._lock:
            if self._terminal_action is None:
                self._terminal_action = "pause"
        self.interrupt_controller.interrupt_from_signal()

    def cancel(self) -> None:
        """请求当前 Run 安全停止，并在收束时终结活动 Plan。"""
        with self._lock:
            self._terminal_action = "cancel"
        self.interrupt_controller.interrupt()

    def steer(self, content: str) -> SteeringQuery:
        """按 FIFO 保存 Query，等待下一个完整 Turn 边界应用。"""
        return self.steering_mailbox.submit(content)

    @property
    def terminal_action(self) -> TerminalControlAction | None:
        """返回当前最高优先级的 Run 终止动作。"""
        with self._lock:
            return self._terminal_action
