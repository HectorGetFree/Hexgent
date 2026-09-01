"""Hexgent Web 协调层测试；不访问真实模型 API。"""

import asyncio
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from hexgent.events import AgentLoopResult, RunFinishedEvent, TextDeltaEvent
from hexgent.plan import InMemoryPlanStore
from hexgent.run_control import RunControl

from backend.event_serializer import event_payload
from backend.errors import WebError
from backend.run_manager import RunManager, transcript_messages
from backend.app import create_app
from backend.settings import WebSettings


class FakeEngine:
    """只提供 RunManager 所依赖的最小 AgentEngine 协议。"""

    def __init__(self, workdir: Path, permission_mode: str, *, gate: asyncio.Event | None = None):
        """保存测试工作区、权限模式和可选执行闸门。"""
        self.workdir = workdir
        self.session = SimpleNamespace(settings=SimpleNamespace(permission_mode=permission_mode))
        self.tool_context = SimpleNamespace(plan_store=InMemoryPlanStore())
        self.gate = gate
        self.received_controls: list[RunControl] = []

    def run(self, query: str, *, run_control: RunControl, user_confirmation):
        """生成文本和最终事件，并响应测试触发的 Pause 或 Cancel。"""
        self.received_controls.append(run_control)
        yield TextDeltaEvent(query)
        if self.gate is not None:
            while not self.gate.is_set() and not run_control.interrupt_controller.interrupted:
                import time
                time.sleep(0.005)
        action = run_control.terminal_action
        status = "cancelled" if action == "cancel" else ("interrupted" if action == "pause" else "completed")
        yield RunFinishedEvent(AgentLoopResult(query, 0.01, status=status))


class BackendTest(unittest.IsolatedAsyncioTestCase):
    """验证 RunManager、SSE、工作区边界和 CORS 协议。"""

    def setUp(self) -> None:
        """为每个测试创建使用 FakeEngine 的独立 RunManager。"""
        self.workspace = Path(".").resolve()
        self.engines: list[FakeEngine] = []

        def factory(workdir, permission_mode):
            """创建并记录当前测试使用的 FakeEngine。"""
            engine = FakeEngine(workdir, permission_mode)
            self.engines.append(engine)
            return engine

        self.manager = RunManager(self.workspace, engine_factory=factory, heartbeat_seconds=0.01)

    async def asyncTearDown(self) -> None:
        """测试结束后安全收束仍在运行的后台任务。"""
        await self.manager.shutdown()

    async def test_run_is_async_and_events_have_strict_sequence(self) -> None:
        """后台 Run 应完成且缓存事件序号严格递增。"""
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "检查项目")
        self.assertIn(run.status, {"pending", "running", "completed"})
        await run.task
        self.assertEqual(run.status, "completed")
        self.assertEqual([event.sequence for event in run.events], [1, 2])
        self.assertEqual([event.type for event in run.events], ["text_delta", "run_finished"])

    def test_transcript_projects_only_user_visible_conversation(self) -> None:
        """刷新历史应恢复用户与 Assistant 文本，并排除工具协议消息。"""
        records = [
            {"sequence": 1, "type": "user_query", "created_at": "2026-09-01T10:00:00+08:00", "payload": {"content": "你好"}},
            {"sequence": 2, "type": "message", "created_at": "2026-09-01T10:00:01+08:00", "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "你好，有什么可以帮你？"}]}},
            {"sequence": 3, "type": "message", "created_at": "2026-09-01T10:00:02+08:00", "payload": {"type": "function_call_output", "output": "内部工具结果"}},
            {"sequence": 4, "type": "steering_query_applied", "created_at": "2026-09-01T10:00:03+08:00", "payload": {"content": "继续检查"}},
        ]
        self.assertEqual(
            [(message["role"], message["content"]) for message in transcript_messages(records)],
            [
                ("user", "你好"),
                ("assistant", "你好，有什么可以帮你？"),
                ("user", "继续检查"),
            ],
        )

    async def test_same_session_rejects_second_active_run(self) -> None:
        """同一 Session 已有活跃 Run 时必须返回冲突。"""
        gate = asyncio.Event()
        self.engines.clear()
        self.manager._engine_factory = lambda workdir, mode: FakeEngine(workdir, mode, gate=gate)
        session = await self.manager.create_session(None, "default")
        first = await self.manager.start_run(session.session_id, "第一个")
        await asyncio.sleep(0)
        with self.assertRaises(WebError) as raised:
            await self.manager.start_run(session.session_id, "第二个")
        self.assertEqual(raised.exception.status_code, 409)
        await self.manager.pause_run(first.run_id)
        await first.task
        self.assertEqual(first.status, "paused")

    async def test_registered_run_immediately_accepts_steering(self) -> None:
        """创建接口返回的 Run 应已可 Steering，不能停在前端无法推进的 pending。"""
        gate = asyncio.Event()
        self.manager._engine_factory = lambda workdir, mode: FakeEngine(workdir, mode, gate=gate)
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "初始任务")

        self.assertEqual(run.status, "running")
        steering = await self.manager.steer_run(run.run_id, "补充要求")
        self.assertEqual(steering.content, "补充要求")

        await self.manager.cancel_run(run.run_id)
        await run.task

    async def test_cancel_overrides_pause_before_safe_finish(self) -> None:
        """安全收束前到达的 Cancel 应覆盖先前 Pause。"""
        gate = asyncio.Event()
        self.manager._engine_factory = lambda workdir, mode: FakeEngine(workdir, mode, gate=gate)
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "任务")
        await asyncio.sleep(0)
        await self.manager.pause_run(run.run_id)
        await self.manager.cancel_run(run.run_id)
        await run.task
        self.assertEqual(run.status, "cancelled")

    async def test_pause_callback_does_not_block_fastapi_event_loop(self) -> None:
        """底层流关闭发生阻塞时，事件循环仍须能够继续推送 SSE 和处理请求。"""
        gate = asyncio.Event()
        self.manager._engine_factory = lambda workdir, mode: FakeEngine(workdir, mode, gate=gate)
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "长任务")
        await asyncio.sleep(0)

        callback_started = Event()
        release_callback = Event()

        def blocking_close() -> None:
            """模拟可能阻塞的模型 SDK stream.close()。"""
            callback_started.set()
            release_callback.wait(timeout=1)

        run.run_control.interrupt_controller.add_callback(blocking_close)
        pause_task = asyncio.create_task(self.manager.pause_run(run.run_id))

        # 若 Pause 仍在事件循环内同步 close，下面的 sleep 无法获得执行机会。
        await asyncio.sleep(0.02)
        self.assertTrue(callback_started.is_set())
        self.assertTrue(run.task.done())

        release_callback.set()
        await pause_task
        await run.task
        self.assertEqual(run.status, "paused")

    async def test_sse_replays_only_events_after_last_id(self) -> None:
        """SSE 重连只回放 Last-Event-ID 之后的缓存事件。"""
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "回放")
        await run.task
        chunks = [chunk async for chunk in self.manager.stream_events(run.run_id, 1)]
        self.assertEqual(len(chunks), 1)
        self.assertIn("id: 2", chunks[0])
        self.assertIn("event: run_finished", chunks[0])

    async def test_sse_yields_text_delta_before_run_finishes(self) -> None:
        """文本 Delta 必须在最终事件之前到达订阅者，不能等 Run 完成后一次回放。"""
        gate = asyncio.Event()
        self.manager._engine_factory = lambda workdir, mode: FakeEngine(workdir, mode, gate=gate)
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "流式内容")

        stream = self.manager.stream_events(run.run_id, 0)
        first_chunk = await anext(stream)
        self.assertIn("event: text_delta", first_chunk)
        self.assertFalse(run.task.done())

        gate.set()
        remaining = [chunk async for chunk in stream]
        await run.task
        self.assertTrue(any("event: run_finished" in chunk for chunk in remaining))

    async def test_workdir_is_restricted_to_configured_workspace(self) -> None:
        """浏览器不能创建指向白名单以外目录的 Session。"""
        with self.assertRaises(WebError) as raised:
            await self.manager.create_session("/tmp", "default")
        self.assertEqual(raised.exception.code, "workdir_not_allowed")

    def test_unknown_event_is_not_implicitly_serialized(self) -> None:
        """未知领域事件不能因通用序列化而意外暴露内部字段。"""
        with self.assertRaises(TypeError):
            event_payload(object())  # type: ignore[arg-type]

    def test_cors_allows_configured_frontend_origin_only(self) -> None:
        """CORS 预检只允许显式配置的前端来源。"""
        from fastapi.testclient import TestClient

        app = create_app(
            manager=self.manager,
            settings=WebSettings(("https://app.hexgent.example",)),
        )
        with TestClient(app) as client:
            allowed = client.options(
                "/api/sessions",
                headers={
                    "Origin": "https://app.hexgent.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
            denied = client.options(
                "/api/sessions",
                headers={
                    "Origin": "https://unknown.example",
                    "Access-Control-Request-Method": "POST",
                },
            )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(
            allowed.headers["access-control-allow-origin"],
            "https://app.hexgent.example",
        )
        self.assertNotIn("access-control-allow-origin", denied.headers)


if __name__ == "__main__":
    unittest.main()
