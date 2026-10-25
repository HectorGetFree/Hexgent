"""Agent Run 的底层中断与暂停、取消、Steering 业务控制。"""

from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Literal
from uuid import uuid4


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

    # interrupt 让主模型请求让位并关闭后续工具准入，不取消整个 Run。
    mode: Literal["boundary", "interrupt"] = "boundary"
    command_id: str | None = None


class SteeringMailbox:
    """在线程之间按 FIFO 保存当前 Run 的 Steering Query。"""

    def __init__(self) -> None:
        # 尚未被 Agent Turn 应用的 Query 队列。
        self._queries: deque[SteeringQuery] = deque()

        # 下一条 Steering Query 使用的当前 Run 内递增编号。
        self._next_sequence = 1

        # 保护交互线程提交与 Agent 线程批量取出之间的顺序一致性。
        self._lock = Lock()

    def submit(self, content: str, *, mode: Literal["boundary", "interrupt"] = "boundary", command_id: str | None = None) -> SteeringQuery:
        """把非空原始 Query 追加到 FIFO 尾部。"""
        if not content.strip():
            raise ValueError("Steering Query 不能为空")
        if mode not in {"boundary", "interrupt"}:
            raise ValueError("Steering 模式必须为 boundary 或 interrupt")
        with self._lock:
            if command_id is not None:
                for previous in self._queries:
                    if previous.command_id == command_id:
                        if (previous.content, previous.mode) != (content, mode):
                            raise ValueError("IDEMPOTENCY_CONFLICT：纠偏命令输入不一致")
                        return previous
            query = SteeringQuery(self._next_sequence, content, mode, command_id)
            self._next_sequence += 1
            self._queries.append(query)
            return query

    def drain(self) -> list[SteeringQuery]:
        """原子取出当前已经到达的全部 Query，并保持提交顺序。"""
        with self._lock:
            queries = list(self._queries)
            self._queries.clear()
            return queries

    def snapshot(self) -> tuple[SteeringQuery, ...]:
        """返回锁保护的队列快照，不消费待应用输入。"""
        with self._lock:
            return tuple(self._queries)

    @property
    def has_pending(self) -> bool:
        """返回是否存在等待注入的 Steering Query。"""
        with self._lock:
            return bool(self._queries)

    @property
    def has_interrupting_input(self) -> bool:
        """检查尚未消费的输入是否要求当前模型请求让位。"""
        with self._lock:
            return any(query.mode == "interrupt" for query in self._queries)


class RunControl:
    """把业务控制意图与一次性底层中断机制组合为 Run 控制入口。"""

    def __init__(
        self,
        interrupt_controller: InterruptController | None = None,
    ) -> None:
        # Pause 和 Cancel 用于唤醒当前模型流并阻止后续工具启动。
        self.interrupt_controller = interrupt_controller or InterruptController()

        # 默认仍在完整边界应用；interrupt 同时让位 MAIN 并停止后续工具准入。
        self.steering_mailbox = SteeringMailbox()

        # 当前 Run 已请求的终止动作；Cancel 可以覆盖先到达的 Pause。
        self._terminal_action: TerminalControlAction | None = None

        # 保护交互线程与 Agent 线程之间的终止动作读写。
        self._lock = Lock()

        # 请求级控制器随主请求换代；Run 级 Pause/Cancel 信号永不复位。
        self._model_request_controller: InterruptController | None = None
        self._model_request_steerable = False
        self._tool_batch_controller = None
        self._steering_session = None

    def bind_steering_session(self, session) -> None:
        """在 Run 入口绑定权威日志；绑定前队列也先落盘再交给执行线程。"""
        with self._lock:
            if self._steering_session is not None and self._steering_session is not session:
                raise RuntimeError("RunControl 不能跨 Session 复用")
            self._steering_session = session
            for query in self.steering_mailbox.snapshot():
                session.record_steering_intent(query.command_id, query.content, query.mode)

    def restore_steering(self, session) -> None:
        """恢复未应用意图；已提交消息但缺 marker 的情况仍交给安全边界处理。"""
        self.bind_steering_session(session)
        with self._lock:
            queued = {q.command_id for q in self.steering_mailbox.snapshot()}
            for intent in session.steering_intents():
                identity = intent["command_id"]
                if identity not in queued and not session.has_runtime_effect(identity, "steering_applied"):
                    self.steering_mailbox.submit(intent["instruction"],
                        mode=intent.get("mode", "boundary"), command_id=identity)
                    queued.add(identity)

    @contextmanager
    def tool_batch(self):
        controller = InterruptController()
        gate = ToolDispatchGate(self, controller)
        with self._lock:
            if self._tool_batch_controller is not None:
                raise RuntimeError("同一 Run 不允许重叠的工具批次")
            self._tool_batch_controller = controller
            pending = self.steering_mailbox.has_interrupting_input
        remove = self.interrupt_controller.add_callback(controller.interrupt)
        try:
            if pending:
                controller.interrupt()
            yield gate
        finally:
            with self._lock:
                gate.closed = True
                self._tool_batch_controller = None
            remove()

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

    def steer(self, content: str, *, mode: Literal["boundary", "interrupt"] = "boundary", command_id: str | None = None) -> SteeringQuery:
        """先持久化再登记 FIFO 纠偏；interrupt 唤醒 MAIN 并关闭工具准入。"""
        # 1. 入队和绑定目标请求互斥，避免输入落在两个请求之间而丢失唤醒。
        with self._lock:
            if not isinstance(content, str) or not content.strip() or mode not in {"boundary", "interrupt"}:
                raise ValueError("纠偏内容或模式无效")
            command_id = command_id or str(uuid4())
            if self._steering_session is not None:
                fresh = self._steering_session.record_steering_intent(command_id, content, mode)
                if not fresh:
                    return SteeringQuery(0, content, mode, command_id)
            query = self.steering_mailbox.submit(content, mode=mode, command_id=command_id)
            request_controller = (
                self._model_request_controller
                if mode == "interrupt" and self._model_request_steerable else None
            )
            tool_controller = self._tool_batch_controller if mode == "interrupt" else None
        # 2. 关闭流可能阻塞，不在控制锁内执行资源回调。
        if request_controller is not None:
            request_controller.interrupt()
        if tool_controller is not None:
            tool_controller.interrupt()
        return query

    def drain_steering(self) -> list[SteeringQuery]:
        """与终止意图仲裁后取输入；Pause/Cancel 已登记时不自动消费。"""
        with self._lock:
            if self._terminal_action is not None or self.interrupt_controller.interrupted:
                return []
            return self.steering_mailbox.drain()

    @contextmanager
    def model_request(self, *, steerable: bool = True) -> Iterator[InterruptController]:
        """为一次主模型调用（含其内部重试）绑定独立的让位控制器。"""
        request_controller = InterruptController()
        with self._lock:
            if self._model_request_controller is not None:
                raise RuntimeError("同一 Run 不允许重叠的主模型请求")
            self._model_request_controller = request_controller
            self._model_request_steerable = steerable
            pending = steerable and self.steering_mailbox.has_interrupting_input
        remove = self.interrupt_controller.add_callback(request_controller.interrupt)
        try:
            if pending:
                request_controller.interrupt()
            yield request_controller
        finally:
            with self._lock:
                self._model_request_controller = None
                self._model_request_steerable = False
            remove()

    @property
    def terminal_action(self) -> TerminalControlAction | None:
        """返回当前最高优先级的 Run 终止动作。"""
        with self._lock:
            return self._terminal_action


class ToolDispatchGate:
    """与纠偏登记共用准入锁；不持锁运行 Handler，也不强杀已准入线程。"""

    def __init__(self, control, controller):
        self.control, self.controller, self.closed = control, controller, False

    @property
    def steering_requested(self):
        return (self.control.terminal_action is None
                and not self.control.interrupt_controller.interrupted
                and self.control.steering_mailbox.has_interrupting_input)

    def close(self):
        with self.control._lock:
            self.closed = True

    def claim(self, record_dispatch):
        with self.control._lock:
            if (self.closed or self.controller.interrupted or self.control._terminal_action is not None
                    or self.control.interrupt_controller.interrupted
                    or self.control.steering_mailbox.has_interrupting_input):
                return False
            record_dispatch()
            return True
