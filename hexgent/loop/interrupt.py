"""Agent Run 当前活跃轮次的协作式中断控制。"""

from collections.abc import Callable
from threading import Event, Lock, Thread


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

        # 阻塞资源清理发生在锁外，避免回调反向访问 Controller 时死锁。
        self._invoke_callbacks(callbacks)

    def interrupt_from_signal(self) -> None:
        """从进程信号处理器登记中断，并异步唤醒阻塞资源。"""
        callbacks = self._claim_interrupt()
        if callbacks is None or not callbacks:
            return

        # 信号处理器不能同步关闭网络流，避免主线程卡在 SDK 资源清理中。
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
