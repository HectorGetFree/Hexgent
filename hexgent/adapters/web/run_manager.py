"""在网络请求与同步 AgentEngine 之间管理 Session、Run 和事件流。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any
from uuid import uuid4

from ...agent_engine import AgentEngine
from ...config import HexgentConfig
from ...events import AgentEvent, RunFinishedEvent
from ...run_control import RunControl
from ...tool.tool_permission_pipeline import (
    PermissionConfig,
    PermissionMode,
    UserConfirmationResult,
)
from .errors import WebAdapterError
from .event_serializer import BEIJING_TIMEZONE, build_web_event, serialize_agent_event
from .schemas import RunResponse, RunStatus, SessionResponse


TERMINAL_RUN_STATUSES = {
    "paused",
    "cancelled",
    "completed",
    "blocked",
    "incomplete",
    "limit_reached",
    "failed",
}

# 网络审批不能无限占用工具线程；超时后按“调用方不可用”交给权限管线收束。
PERMISSION_WAIT_TIMEOUT_SECONDS = 300.0


@dataclass
class PendingPermission:
    """一个正在等待网络调用方回应的同步权限请求。"""

    request_id: str
    tool_name: str
    message: str
    resolved: Event = field(default_factory=Event)
    decision: UserConfirmationResult | None = None
    lock: Lock = field(default_factory=Lock)

    def resolve(self, decision: UserConfirmationResult) -> bool:
        """只接受第一次审批结果，并唤醒等待中的工具线程。"""
        with self.lock:
            if self.resolved.is_set():
                return False
            self.decision = decision
            self.resolved.set()
            return True


@dataclass
class SessionHandle:
    """一个网络 Session 独占的 AgentEngine。"""

    session_id: str
    engine: AgentEngine
    permission_mode: PermissionMode
    active_run_id: str | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class RunHandle:
    """一次 Agent Run 的控制器、事件缓存与网络状态。"""

    run_id: str
    session_id: str
    query: str
    run_control: RunControl
    status: RunStatus = "pending"
    created_at: datetime = field(
        default_factory=lambda: datetime.now(BEIJING_TIMEZONE)
    )
    finished_at: datetime | None = None
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    permissions: dict[str, PendingPermission] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


class RunManager:
    """把 HTTP 生命周期转换为同进程 AgentEngine 调用。"""

    def __init__(
        self,
        config: HexgentConfig,
        workdir: Path,
        *,
        engine_factory: Callable[[HexgentConfig, Path], AgentEngine] = AgentEngine,
    ) -> None:
        # 所有 Web Session 共享服务启动配置，但各自拥有独立 AgentEngine。
        self.config = config
        self.workdir = workdir.resolve()
        self._engine_factory = engine_factory
        self._sessions: dict[str, SessionHandle] = {}
        self._runs: dict[str, RunHandle] = {}
        self._registry_lock = asyncio.Lock()

    async def create_session(
        self,
        permission_mode: PermissionMode,
    ) -> SessionResponse:
        """使用服务端固定工作区创建一个隔离 Session。"""
        # 1. 权限 Mode 只覆盖新 Session 默认值，不改变服务全局配置。
        session_config = replace(
            self.config,
            permission=PermissionConfig(permission_mode),
        )
        session_id = f"session_{uuid4().hex[:12]}"
        handle = SessionHandle(
            session_id,
            self._engine_factory(session_config, self.workdir),
            permission_mode,
        )

        # 2. 注册表修改统一在异步锁内完成。
        async with self._registry_lock:
            self._sessions[session_id] = handle
        return self._session_response(handle)

    async def get_session(self, session_id: str) -> SessionResponse:
        """返回一个 Session 的当前网络状态。"""
        return self._session_response(self._require_session(session_id))

    async def start_run(self, session_id: str, query: str) -> RunResponse:
        """立即注册 Run，并在后台线程中消费同步 Agent 事件流。"""
        session = self._require_session(session_id)
        normalized_query = query.strip()
        if not normalized_query:
            raise WebAdapterError(422, "empty_query", "用户 Query 不能为空")

        # 1. 同一 Session 只允许一个活跃 Run；冲突交给调用方决定如何处理。
        async with session.lock:
            if session.active_run_id is not None:
                raise WebAdapterError(
                    409,
                    "run_already_active",
                    "当前 Session 已有正在运行的 Agent Run",
                    {"active_run_id": session.active_run_id},
                )
            run_id = f"run_{uuid4().hex[:12]}"
            handle = RunHandle(run_id, session_id, query, RunControl())
            self._runs[run_id] = handle
            session.active_run_id = run_id

        # 2. 后台 asyncio Task 再把同步 Generator 放入工作线程执行。
        handle.task = asyncio.create_task(
            self._run_worker(session, handle),
            name=f"hexgent-{run_id}",
        )
        return self._run_response(handle)

    async def get_run(self, run_id: str) -> RunResponse:
        """返回一个 Run 的当前生命周期状态。"""
        return self._run_response(self._require_run(run_id))

    async def pause(self, run_id: str) -> RunResponse:
        """请求 Core 在最近安全边界暂停当前 Run。"""
        handle = self._require_active_run(run_id)
        handle.run_control.pause()
        self._resolve_pending_permissions(handle, "unavailable")
        return self._run_response(handle)

    async def cancel(self, run_id: str) -> RunResponse:
        """请求 Core 安全取消当前 Run。"""
        handle = self._require_active_run(run_id)
        handle.run_control.cancel()
        self._resolve_pending_permissions(handle, "unavailable")
        return self._run_response(handle)

    async def steer(self, run_id: str, query: str) -> RunResponse:
        """把原始 Query 按 FIFO 提交到当前 Run 的 SteeringMailbox。"""
        handle = self._require_active_run(run_id)
        handle.run_control.steer(query)
        return self._run_response(handle)

    async def resolve_permission(
        self,
        run_id: str,
        request_id: str,
        decision: UserConfirmationResult,
    ) -> RunResponse:
        """回应一个仍在等待的 Web 工具审批。"""
        handle = self._require_active_run(run_id)
        pending = handle.permissions.get(request_id)
        if pending is None:
            raise WebAdapterError(404, "permission_not_found", "权限请求不存在")
        if not pending.resolve(decision):
            raise WebAdapterError(409, "permission_already_resolved", "权限请求已经处理")
        return self._run_response(handle)

    async def wait_for_events(
        self,
        run_id: str,
        after_sequence: int,
        *,
        timeout_seconds: float = 15.0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """等待新事件或终态，支持 SSE 断线后的内存回放。"""
        handle = self._require_run(run_id)
        async with handle.condition:
            if len(handle.events) <= after_sequence and not self._is_terminal(handle):
                try:
                    await asyncio.wait_for(
                        handle.condition.wait(),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
            events = list(handle.events[after_sequence:])
            return events, self._is_terminal(handle)

    async def shutdown(self) -> None:
        """服务关闭时请求所有活跃 Run 暂停并等待后台任务收束。"""
        active = [
            handle
            for handle in self._runs.values()
            if not self._is_terminal(handle)
        ]
        for handle in active:
            handle.run_control.pause()
            self._resolve_pending_permissions(handle, "unavailable")
        tasks = [handle.task for handle in active if handle.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_worker(
        self,
        session: SessionHandle,
        handle: RunHandle,
    ) -> None:
        """把同步 Agent Generator 放入线程，并收束异常终态。"""
        loop = asyncio.get_running_loop()
        handle.status = "running"
        try:
            await asyncio.to_thread(self._consume_run, session, handle, loop)
            if not self._is_terminal(handle):
                await self._mark_failed(handle, "Agent 事件流结束但没有 RunFinishedEvent")
        except Exception as error:
            await self._mark_failed(handle, f"Agent Run 执行失败：{error}")
        finally:
            self._resolve_pending_permissions(handle, "unavailable")
            async with session.lock:
                if session.active_run_id == handle.run_id:
                    session.active_run_id = None

    def _consume_run(
        self,
        session: SessionHandle,
        handle: RunHandle,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """在线程中消费 Core 事件，并同步提交到主事件循环。"""
        confirmation = self._build_confirmation(handle, loop)
        for event in session.engine.run(
            handle.query,
            user_confirmation=confirmation,
            run_control=handle.run_control,
        ):
            future = asyncio.run_coroutine_threadsafe(
                self._publish_agent_event(handle, event),
                loop,
            )
            future.result()

    def _build_confirmation(
        self,
        handle: RunHandle,
        loop: asyncio.AbstractEventLoop,
    ) -> Callable[[str, str], UserConfirmationResult]:
        """构造可以由 HTTP 审批接口唤醒的同步权限确认端口。"""
        def confirm(tool_name: str, message: str) -> UserConfirmationResult:
            # 1. 先在事件循环注册请求并发布 permission_requested 事件。
            future = asyncio.run_coroutine_threadsafe(
                self._create_permission_request(handle, tool_name, message),
                loop,
            )
            pending = future.result()

            # 2. 工具线程阻塞等待网络决定；Pause/Cancel 会将其唤醒为 unavailable。
            if not pending.resolved.wait(PERMISSION_WAIT_TIMEOUT_SECONDS):
                pending.resolve("unavailable")
            return pending.decision or "unavailable"

        return confirm

    async def _create_permission_request(
        self,
        handle: RunHandle,
        tool_name: str,
        message: str,
    ) -> PendingPermission:
        """注册并发布一次 Web 权限确认请求。"""
        request_id = f"permission_{uuid4().hex[:12]}"
        pending = PendingPermission(request_id, tool_name, message)
        handle.permissions[request_id] = pending
        await self._publish_web_event(
            handle,
            "permission_requested",
            {
                "request_id": request_id,
                "tool_name": tool_name,
                "message": message,
            },
        )
        return pending

    async def _publish_agent_event(
        self,
        handle: RunHandle,
        event: AgentEvent,
    ) -> None:
        """按 Run 内顺序追加一个 Core 事件并更新网络终态。"""
        async with handle.condition:
            serialized = serialize_agent_event(
                event,
                run_id=handle.run_id,
                sequence=len(handle.events) + 1,
            )
            handle.events.append(serialized)
            if isinstance(event, RunFinishedEvent):
                handle.status = self._map_terminal_status(event.result.status)
                handle.finished_at = datetime.now(BEIJING_TIMEZONE)
            handle.condition.notify_all()

    async def _publish_web_event(
        self,
        handle: RunHandle,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """按同一序列追加 Adapter 自身产生的事件。"""
        async with handle.condition:
            handle.events.append(
                build_web_event(
                    event_type,
                    payload,
                    run_id=handle.run_id,
                    sequence=len(handle.events) + 1,
                )
            )
            handle.condition.notify_all()

    async def _mark_failed(self, handle: RunHandle, message: str) -> None:
        """把未处理异常转换为可观察的网络终态。"""
        if self._is_terminal(handle):
            return
        handle.status = "failed"
        handle.error = message
        handle.finished_at = datetime.now(BEIJING_TIMEZONE)
        await self._publish_web_event(handle, "run_failed", {"message": message})

    def _require_session(self, session_id: str) -> SessionHandle:
        """取得 Session，或形成稳定 404 错误。"""
        handle = self._sessions.get(session_id)
        if handle is None:
            raise WebAdapterError(404, "session_not_found", "Session 不存在")
        return handle

    def _require_run(self, run_id: str) -> RunHandle:
        """取得 Run，或形成稳定 404 错误。"""
        handle = self._runs.get(run_id)
        if handle is None:
            raise WebAdapterError(404, "run_not_found", "Agent Run 不存在")
        return handle

    def _require_active_run(self, run_id: str) -> RunHandle:
        """取得仍允许控制的 Run。"""
        handle = self._require_run(run_id)
        if self._is_terminal(handle):
            raise WebAdapterError(409, "run_not_active", "Agent Run 已经结束")
        return handle

    @staticmethod
    def _is_terminal(handle: RunHandle) -> bool:
        """判断 Run 是否已经形成网络终态。"""
        return handle.status in TERMINAL_RUN_STATUSES

    @staticmethod
    def _map_terminal_status(status: str) -> RunStatus:
        """将 Core 终态映射为网络 Run 状态。"""
        if status == "interrupted":
            return "paused"
        return status  # type: ignore[return-value]

    @staticmethod
    def _resolve_pending_permissions(
        handle: RunHandle,
        decision: UserConfirmationResult,
    ) -> None:
        """批量唤醒当前 Run 中仍在等待的权限请求。"""
        for pending in handle.permissions.values():
            pending.resolve(decision)

    @staticmethod
    def _session_response(handle: SessionHandle) -> SessionResponse:
        """构造 Session 网络响应。"""
        return SessionResponse(
            session_id=handle.session_id,
            permission_mode=handle.permission_mode,
            active_run_id=handle.active_run_id,
        )

    @staticmethod
    def _run_response(handle: RunHandle) -> RunResponse:
        """构造 Run 网络响应。"""
        return RunResponse(
            run_id=handle.run_id,
            session_id=handle.session_id,
            query=handle.query,
            status=handle.status,
            created_at=handle.created_at.isoformat(),
            finished_at=(
                handle.finished_at.isoformat()
                if handle.finished_at is not None
                else None
            ),
            error=handle.error,
        )
