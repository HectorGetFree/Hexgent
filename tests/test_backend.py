"""Hexgent Web 协调层测试；不访问真实模型 API。"""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from hexgent.events import AgentLoopResult, RunFinishedEvent, TextDeltaEvent
from hexgent.plan import InMemoryPlanStore
from hexgent.run_control import RunControl

from backend.event_serializer import event_payload
from backend.errors import WebError
from backend.run_manager import RunManager


class FakeEngine:
    """只提供 RunManager 所依赖的最小 AgentEngine 协议。"""

    def __init__(self, workdir: Path, permission_mode: str, *, gate: asyncio.Event | None = None):
        self.workdir = workdir
        self.session = SimpleNamespace(settings=SimpleNamespace(permission_mode=permission_mode))
        self.tool_context = SimpleNamespace(plan_store=InMemoryPlanStore())
        self.gate = gate
        self.received_controls: list[RunControl] = []

    def run(self, query: str, *, run_control: RunControl, user_confirmation):
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
    def setUp(self) -> None:
        self.workspace = Path(".").resolve()
        self.engines: list[FakeEngine] = []

        def factory(workdir, permission_mode):
            engine = FakeEngine(workdir, permission_mode)
            self.engines.append(engine)
            return engine

        self.manager = RunManager(self.workspace, engine_factory=factory, heartbeat_seconds=0.01)

    async def asyncTearDown(self) -> None:
        await self.manager.shutdown()

    async def test_run_is_async_and_events_have_strict_sequence(self) -> None:
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "检查项目")
        self.assertIn(run.status, {"pending", "running", "completed"})
        await run.task
        self.assertEqual(run.status, "completed")
        self.assertEqual([event.sequence for event in run.events], [1, 2])
        self.assertEqual([event.type for event in run.events], ["text_delta", "run_finished"])

    async def test_same_session_rejects_second_active_run(self) -> None:
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

    async def test_cancel_overrides_pause_before_safe_finish(self) -> None:
        gate = asyncio.Event()
        self.manager._engine_factory = lambda workdir, mode: FakeEngine(workdir, mode, gate=gate)
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "任务")
        await asyncio.sleep(0)
        await self.manager.pause_run(run.run_id)
        await self.manager.cancel_run(run.run_id)
        await run.task
        self.assertEqual(run.status, "cancelled")

    async def test_sse_replays_only_events_after_last_id(self) -> None:
        session = await self.manager.create_session(None, "default")
        run = await self.manager.start_run(session.session_id, "回放")
        await run.task
        chunks = [chunk async for chunk in self.manager.stream_events(run.run_id, 1)]
        self.assertEqual(len(chunks), 1)
        self.assertIn("id: 2", chunks[0])
        self.assertIn("event: run_finished", chunks[0])

    async def test_workdir_is_restricted_to_configured_workspace(self) -> None:
        with self.assertRaises(WebError) as raised:
            await self.manager.create_session("/tmp", "default")
        self.assertEqual(raised.exception.code, "workdir_not_allowed")

    def test_unknown_event_is_not_implicitly_serialized(self) -> None:
        with self.assertRaises(TypeError):
            event_payload(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
