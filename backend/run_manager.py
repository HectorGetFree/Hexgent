"""Session、Run、SSE 缓存和线程桥接的生命周期协调器。"""

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any, AsyncIterator, Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from hexgent.agent_engine import AgentEngine
from hexgent.config import load_config
from hexgent.events import AgentEvent, RunFinishedEvent
from hexgent.run_control import RunControl
from hexgent.tool.tool_permission_pipeline import PermissionConfig, PermissionMode, UserConfirmationResult

from .errors import WebError
from .event_serializer import SerializedEvent, event_payload, serialize_plan
from .schemas import RunStatus


BEIJING = ZoneInfo("Asia/Shanghai")
TERMINAL_STATUSES = {"paused", "cancelled", "completed", "failed"}
logger = logging.getLogger(__name__)


def beijing_now() -> datetime:
    """返回带 Asia/Shanghai 时区的当前时间。"""
    return datetime.now(BEIJING)


@dataclass
class SessionHandle:
    """Web Session 的运行时句柄，一个 Session 独占一个 AgentEngine。"""

    session_id: str
    engine: AgentEngine
    active_run_id: str | None = None
    run_ids: list[str] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class PermissionRequest:
    """连接 Agent 工作线程与 Web 审批接口的一次权限请求。"""

    request_id: str
    run_id: str
    tool_name: str
    reason: str
    permission_mode: PermissionMode
    decision: UserConfirmationResult | None = None
    resolved: Event = field(default_factory=Event)
    lock: Lock = field(default_factory=Lock)


@dataclass
class RunHandle:
    """一次 Agent Run 的状态、控制器、任务和内存事件缓存。"""

    run_id: str
    session_id: str
    query: str
    run_control: RunControl
    status: RunStatus = "pending"
    task: asyncio.Task[None] | None = None
    events: list[SerializedEvent] = field(default_factory=list)
    event_condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    created_at: datetime = field(default_factory=beijing_now)
    finished_at: datetime | None = None
    error: str | None = None


EngineFactory = Callable[[Path, PermissionMode], AgentEngine]


class RunManager:
    """协调 Web Session、Agent Run、权限请求和 SSE 订阅生命周期。"""

    def __init__(self, workspace: Path, *, engine_factory: EngineFactory | None = None, heartbeat_seconds: float = 15.0, permission_timeout_seconds: float = 300.0):
        """使用唯一允许的工作区和可替换 Engine 工厂初始化 Manager。"""
        self.workspace = workspace.resolve()
        self._engine_factory = engine_factory or self._create_engine
        self.heartbeat_seconds = heartbeat_seconds
        self.permission_timeout_seconds = permission_timeout_seconds
        self._sessions: dict[str, SessionHandle] = {}
        self._runs: dict[str, RunHandle] = {}
        self._permissions: dict[str, PermissionRequest] = {}
        self._lock = asyncio.Lock()
        self._accepting_runs = True
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _new_id(prefix: str) -> str:
        """生成带业务前缀的进程内唯一标识。"""
        return f"{prefix}_{uuid4().hex}"

    @staticmethod
    def _create_engine(workspace: Path, permission_mode: PermissionMode) -> AgentEngine:
        """加载工作区配置，并为新 Session 创建独立 AgentEngine。"""
        # Session 权限模式在 Engine 创建前写入配置，保证内部 Session 快照一致。
        config = load_config(workspace)
        config = replace(config, permission=PermissionConfig(permission_mode))
        engine = AgentEngine(config, workspace)
        return engine

    async def create_session(self, workdir: str | None, permission_mode: PermissionMode) -> SessionHandle:
        """校验工作区白名单并注册一个新的 Web Session。"""
        # 1. 浏览器只能选择服务启动时配置的工作区，不能任意访问服务器路径。
        requested = Path(workdir).expanduser().resolve() if workdir else self.workspace
        if requested != self.workspace:
            raise WebError(422, "workdir_not_allowed", "workdir 不在服务配置的工作区白名单中", {"allowed_workdir": str(self.workspace)})
        # 2. 每个 Session 创建独立 Engine，以隔离 Context、Session 和 PlanStore。
        handle = SessionHandle(self._new_id("session"), self._engine_factory(requested, permission_mode))
        # 3. Manager 总表只在异步锁内更新，避免并发创建时破坏字典状态。
        async with self._lock:
            self._sessions[handle.session_id] = handle
        return handle

    async def get_session(self, session_id: str) -> SessionHandle:
        """按 ID 返回 Session；不存在时抛出稳定的 404 WebError。"""
        async with self._lock:
            handle = self._sessions.get(session_id)
        if handle is None:
            raise WebError(404, "session_not_found", "Session 不存在")
        return handle

    async def get_run(self, run_id: str) -> RunHandle:
        """按 ID 返回 Run；不存在时抛出稳定的 404 WebError。"""
        async with self._lock:
            handle = self._runs.get(run_id)
        if handle is None:
            raise WebError(404, "run_not_found", "Run 不存在")
        return handle

    async def start_run(self, session_id: str, query: str) -> RunHandle:
        """为 Session 登记唯一活跃 Run，并调度异步后台任务。"""
        # 1. 关闭阶段先拒绝新任务，避免 lifespan 收束期间继续扩张工作集。
        if not self._accepting_runs:
            raise WebError(503, "service_shutting_down", "服务正在关闭，不再接受新 Run")
        # 2. Session 锁把“检查活跃 Run”和“登记新 Run”合并为一个原子操作。
        session = await self.get_session(session_id)
        async with session.lock:
            if session.active_run_id is not None:
                raise WebError(409, "run_already_active", "当前 Session 已有正在运行的任务", {"active_run_id": session.active_run_id})
            # 3. 每个 Run 使用全新的 RunControl，已触发中断的控制器绝不复用。
            handle = RunHandle(self._new_id("run"), session_id, query, RunControl())
            async with self._lock:
                self._runs[handle.run_id] = handle
            session.active_run_id = handle.run_id
            session.run_ids.append(handle.run_id)
            # 4. create_task 只登记后台任务，因此 HTTP 创建接口可以立即返回。
            handle.task = asyncio.create_task(self._execute_run(handle), name=f"hexgent-{handle.run_id}")
        return handle

    async def _execute_run(self, handle: RunHandle) -> None:
        """在线程中消费同步 Agent 流，并在异步侧完成 Run 收束。"""
        # 1. 保存主事件循环引用，供 Agent 工作线程线程安全地投递事件。
        self._loop = asyncio.get_running_loop()
        handle.status = "running"
        try:
            # 2. 同步 AgentEngine.run() 不能阻塞 FastAPI 事件循环，放入工作线程。
            await asyncio.to_thread(self._consume_agent_events, handle)
            if handle.status not in TERMINAL_STATUSES:
                raise RuntimeError("Agent 事件流结束前没有产生 RunFinishedEvent")
        except Exception as error:
            # 3. Web Worker 异常形成独立失败事件，不伪装为 Agent 工具失败。
            handle.status = "failed"
            handle.error = str(error)
            self._append_serialized(handle, "worker_failed", {"message": str(error)})
        finally:
            # 4. 无论成功失败都唤醒审批、释放 Session 活跃槽并通知 SSE 订阅者。
            handle.finished_at = beijing_now()
            self._resolve_pending_permissions(handle.run_id, "unavailable")
            session = await self.get_session(handle.session_id)
            async with session.lock:
                if session.active_run_id == handle.run_id:
                    session.active_run_id = None
            await self._notify(handle)

    def _consume_agent_events(self, handle: RunHandle) -> None:
        """在工作线程中同步迭代 AgentEvent，并投递回主事件循环。"""
        # 1. Session 的 Engine 仅由当前唯一活跃 Run 驱动。
        session = self._sessions[handle.session_id]
        # 2. 同步权限回调可以阻塞当前工作线程，但不会阻塞 HTTP 事件循环。
        confirmation = self._build_confirmation(handle, session)
        for event in session.engine.run(handle.query, run_control=handle.run_control, user_confirmation=confirmation):
            # 3. asyncio 对象只能在主事件循环中修改，工作线程只负责投递回调。
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._publish_agent_event, handle, event)

    def _publish_agent_event(self, handle: RunHandle, event: AgentEvent) -> None:
        """在主事件循环中序列化 AgentEvent，并根据最终事件确认状态。"""
        event_type, payload = event_payload(event)
        self._append_serialized(handle, event_type, payload)
        if isinstance(event, RunFinishedEvent):
            # 控制接口只登记意图；这里收到最终事件后才确认业务终态。
            status = event.result.status
            handle.status = "paused" if status == "interrupted" else ("cancelled" if status == "cancelled" else ("failed" if status == "failed" else "completed"))

    def _append_serialized(self, handle: RunHandle, event_type: str, payload: dict[str, Any]) -> None:
        """为 Run 追加严格递增的缓存事件，并异步唤醒订阅者。"""
        handle.events.append(SerializedEvent(len(handle.events) + 1, event_type, beijing_now(), handle.run_id, payload))
        asyncio.create_task(self._notify(handle))

    @staticmethod
    async def _notify(handle: RunHandle) -> None:
        """通知当前 Run 的全部 SSE 订阅者重新检查事件和终态。"""
        async with handle.event_condition:
            handle.event_condition.notify_all()

    def _build_confirmation(self, handle: RunHandle, session: SessionHandle):
        """构造供 Agent 工作线程调用的同步 Web 权限审批函数。"""

        def confirm(tool_name: str, reason: str) -> UserConfirmationResult:
            """发布审批事件并阻塞工作线程，直到决定、超时或 Run 中断。"""
            # 1. 工作线程创建线程安全等待对象，主循环负责登记和向前端发布。
            request = PermissionRequest(self._new_id("permission"), handle.run_id, tool_name, reason, session.engine.session.settings.permission_mode)
            assert self._loop is not None
            self._loop.call_soon_threadsafe(self._register_permission, handle, request)
            # 2. 使用事件循环的单调时钟计算超时，不受系统时间调整影响。
            deadline = self._loop.time() + self.permission_timeout_seconds
            while not request.resolved.wait(0.1):
                # 3. Pause/Cancel 必须终止权限等待，否则 Run 无法安全收束。
                if handle.run_control.interrupt_controller.interrupted:
                    return "unavailable"
                if self._loop.time() >= deadline:
                    return "unavailable"
            return request.decision or "unavailable"
        return confirm

    def _register_permission(self, handle: RunHandle, request: PermissionRequest) -> None:
        """在主事件循环登记权限请求，并发布 permission_requested 事件。"""
        self._permissions[request.request_id] = request
        self._append_serialized(handle, "permission_requested", {"request_id": request.request_id, "tool_name": request.tool_name, "reason": request.reason, "permission_mode": request.permission_mode})

    async def decide_permission(self, run_id: str, request_id: str, decision: str) -> PermissionRequest:
        """幂等提交权限决定，并唤醒等待中的 Agent 工作线程。"""
        # 1. 先验证 Run、请求归属和终态，拒绝跨 Run 或迟到审批。
        handle = await self.get_run(run_id)
        request = self._permissions.get(request_id)
        if request is None or request.run_id != run_id:
            raise WebError(404, "permission_not_found", "审批请求不存在")
        if handle.status in TERMINAL_STATUSES:
            raise WebError(409, "run_already_finished", "Run 已结束，不能提交迟到审批")
        # 2. 线程锁保证 HTTP 决定和 Pause/Cancel 唤醒不会互相覆盖。
        with request.lock:
            if request.decision is not None and request.decision != decision:
                raise WebError(409, "permission_already_decided", "审批请求已经作出不同决定")
            request.decision = decision  # type: ignore[assignment]
            request.resolved.set()
        return request

    async def pause_run(self, run_id: str) -> RunHandle:
        """请求运行中的 Run 安全暂停，并解除可能存在的权限等待。"""
        handle = await self._require_running(run_id)
        handle.run_control.pause()
        self._resolve_pending_permissions(run_id, "unavailable")
        return handle

    async def cancel_run(self, run_id: str) -> RunHandle:
        """请求待执行或运行中的 Run 安全取消，并解除权限等待。"""
        handle = await self.get_run(run_id)
        if handle.status not in {"pending", "running"}:
            raise WebError(409, "invalid_run_status", "当前 Run 状态不允许取消", {"status": handle.status})
        handle.run_control.cancel()
        self._resolve_pending_permissions(run_id, "unavailable")
        return handle

    async def steer_run(self, run_id: str, query: str):
        """把原始 Query 写入运行中 Run 的 FIFO SteeringMailbox。"""
        handle = await self._require_running(run_id)
        return handle.run_control.steer(query)

    async def _require_running(self, run_id: str) -> RunHandle:
        """取得运行中的 Run，否则返回带当前状态的 409 错误。"""
        handle = await self.get_run(run_id)
        if handle.status != "running":
            raise WebError(409, "invalid_run_status", "当前 Run 状态不允许此操作", {"status": handle.status})
        return handle

    def _resolve_pending_permissions(self, run_id: str, decision: UserConfirmationResult) -> None:
        """以给定兜底决定唤醒目标 Run 的全部未决权限请求。"""
        for request in self._permissions.values():
            if request.run_id == run_id and not request.resolved.is_set():
                with request.lock:
                    request.decision = decision
                    request.resolved.set()

    async def stream_events(self, run_id: str, last_event_id: int) -> AsyncIterator[str]:
        """先回放缓存，再等待新事件，终态发送完毕后关闭 SSE。"""
        # 1. cursor 表示订阅者最后已经收到的 sequence，负数按 0 处理。
        handle = await self.get_run(run_id)
        cursor = max(last_event_id, 0)
        while True:
            while cursor < len(handle.events):
                # 2. 事件列表下标与从 1 开始的 sequence 对齐，按原顺序回放。
                event = handle.events[cursor]
                cursor = event.sequence
                data = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.sequence}\nevent: {event.type}\ndata: {data}\n\n"
            if handle.status in TERMINAL_STATUSES:
                # 3. 终态 Run 的全部缓存已经发送完成，可以安全关闭连接。
                return
            try:
                # 4. Condition 只负责通知；谓词避免“检查后、等待前”丢失唤醒。
                async with handle.event_condition:
                    await asyncio.wait_for(
                        handle.event_condition.wait_for(
                            lambda: cursor < len(handle.events)
                            or handle.status in TERMINAL_STATUSES
                        ),
                        timeout=self.heartbeat_seconds,
                    )
            except asyncio.TimeoutError:
                # 5. 空闲期间发送心跳，避免浏览器和反向代理关闭长连接。
                yield "event: heartbeat\ndata: {}\n\n"

    async def shutdown(self, timeout: float = 5.0) -> None:
        """停止接收新 Run，请求活跃 Run 暂停并有限等待安全收束。"""
        # 1. 先关闭入口，再快照当前活跃 Run，避免关闭过程中集合继续增长。
        self._accepting_runs = False
        active = [run for run in self._runs.values() if run.status in {"pending", "running"}]
        for run in active:
            # 2. Pause 保留 Context 和 Plan；权限等待也必须同步唤醒。
            run.run_control.pause()
            self._resolve_pending_permissions(run.run_id, "unavailable")
        tasks = [run.task for run in active if run.task is not None]
        if tasks:
            # 3. 只有限等待，不把超时任务伪装成正常完成。
            _done, pending = await asyncio.wait(tasks, timeout=timeout)
            if pending:
                unfinished = [
                    run.run_id
                    for run in active
                    if run.task in pending
                ]
                logger.warning("服务关闭等待超时，仍未安全收束的 Run：%s", unfinished)

    @staticmethod
    def session_dict(handle: SessionHandle) -> dict[str, Any]:
        """把 SessionHandle 投影为稳定的 API 响应字典。"""
        plan = handle.engine.tool_context.plan_store.get_plan()
        return {"session_id": handle.session_id, "workdir": str(handle.engine.workdir), "permission_mode": handle.engine.session.settings.permission_mode, "active_run_id": handle.active_run_id, "recent_run_ids": list(reversed(handle.run_ids[-20:])), "plan": serialize_plan(plan) if plan else None}

    @staticmethod
    def run_dict(handle: RunHandle) -> dict[str, Any]:
        """把 RunHandle 投影为稳定的 API 响应字典。"""
        return {"run_id": handle.run_id, "session_id": handle.session_id, "status": handle.status, "created_at": handle.created_at, "finished_at": handle.finished_at, "error": handle.error}
