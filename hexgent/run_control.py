"""Agent Run 的底层中断与暂停、取消、Steering 业务控制。"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Literal


class InterruptController:
    """在线程之间传播一次不可逆的当前 Run 中断请求。"""

    def __init__(self) -> None:
        # 中断事件允许模型流和工具调度线程安全地观察请求。
        self._event = Event()
        # 保护多个调用方同时请求中断时的一次性触发语义。
        self._lock = Lock()
        # 中断发生时用于关闭模型流等阻塞资源的回调。
        self._callbacks: list[Callable[[], None]] = []

    def interrupt(self) -> None:
        """请求当前活跃轮次在最近安全边界结束。"""
        callbacks = self._claim_interrupt()
        if callbacks is None:
            return
        self._invoke_callbacks(callbacks)

    def interrupt_from_signal(self) -> None:
        """从进程信号处理器登记中断，并异步唤醒阻塞资源。"""
        callbacks = self._claim_interrupt()
        if callbacks is None or not callbacks:
            return
        Thread(
            target=self._invoke_callbacks,
            args=(callbacks,),
            name="hexgent-interrupt-wakeup",
            daemon=True,
        ).start()

    def _claim_interrupt(self) -> list[Callable[[], None]] | None:
        """原子登记一次底层中断，并取走需要唤醒的资源回调。"""
        with self._lock:
            if self._event.is_set():
                return None
            self._event.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        return callbacks

    @staticmethod
    def _invoke_callbacks(callbacks: list[Callable[[], None]]) -> None:
        """尽力执行用于唤醒模型流等阻塞资源的回调。"""
        for callback in callbacks:
            try:
                callback()
            except Exception:
                # 中断回调只负责尽力唤醒阻塞操作，失败仍由轮询边界收束。
                continue

    def add_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """注册一次中断回调，并返回可幂等调用的移除函数。"""
        with self._lock:
            if self._event.is_set():
                should_call_now = True
            else:
                self._callbacks.append(callback)
                should_call_now = False
        if should_call_now:
            callback()

        def remove_callback() -> None:
            """在资源正常结束后移除尚未触发的回调。"""
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)

        return remove_callback

    @property
    def interrupted(self) -> bool:
        """返回当前 Run 是否已经收到中断请求。"""
        return self._event.is_set()


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
