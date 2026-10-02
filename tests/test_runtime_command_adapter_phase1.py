"""验证 WS logical run 在 Runtime 重启后的持久化关联与续跑边界。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.adapters.web.run_manager import RunManager
from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.context import ContextManager
from hexgent.events import AgentLoopResult, RunFinishedEvent
from hexgent.integration.protocol import RuntimeCommand
from hexgent.integration.runtime_adapter import RuntimeCommandAdapter
from hexgent.loop.loop_state import LoopState
from hexgent.session import Session
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.plan import InMemoryPlanStore


class _ProbeEngine:
    """以真实 SessionRestorer 为基础，避免测试依赖外部模型服务。"""

    def __init__(self, engine: AgentEngine) -> None:
        self._engine = engine
        self.workdir = engine.workdir
        self.session = engine.session
        self.context_manager = engine.context_manager
        self.tool_context = engine.tool_context
        self.requests: list[RunRequest] = []

    @property
    def has_continuable_run(self) -> bool:
        """透传真实 Engine 从 Checkpoint 恢复出的可续跑状态。"""
        return self._engine.has_continuable_run

    def run(self, request: RunRequest, **kwargs: object):
        """记录真实 Session 事实，并模拟一个可继续的中断终态。"""
        self._engine._apply_pending_steering_intents()
        self.requests.append(request)
        if request.intent == "submit":
            query = self.context_manager.append_user_query(request.query or "")
            self.session.record_user_query(query)
        self.session.bind_runtime_run(
            str(kwargs["workspace_run_id"]),
            str(kwargs["conversation_id"]),
        )
        self.session.start_run(limits_enabled=False, plan=None)
        state = LoopState(self.context_manager, self.tool_context)
        self.session.finish_run(
            status="interrupted",
            stop_reason="test_pause",
            elapsed_seconds=0.01,
            output_text="暂停",
            plan=None,
            context_manager=self.context_manager,
            loop_state=state,
        )
        yield RunFinishedEvent(AgentLoopResult("暂停", 0.01, status="interrupted"))

    def cancel_pending_task(self, reason: str = "用户取消暂停任务") -> None:
        """测试不覆盖取消，仅保持 RunManager 所需接口。"""
        self._engine.cancel_pending_task(reason)


class _ProbeEngineFactory:
    """创建新 Engine 时保留真实 hydrate 路径并记录恢复次数。"""

    def __init__(self) -> None:
        self.restored_engines: list[_ProbeEngine] = []

    def __call__(self, config: HexgentConfig, workdir: Path) -> _ProbeEngine:
        return _ProbeEngine(AgentEngine.create(config, workdir))

    def restore(
        self,
        config: HexgentConfig,
        workdir: Path,
        session_id: str,
    ) -> _ProbeEngine:
        engine = _ProbeEngine(AgentEngine.restore(config, workdir, session_id))
        self.restored_engines.append(engine)
        return engine


def _command(
    command_id: str,
    command_type: str,
    run_id: str = "R1",
    conversation_id: str = "C1",
    allowed_tools: list[str] | None = None,
) -> RuntimeCommand:
    """构造最小 Runtime Command。"""
    payload = (
        {
            "instruction": "继续处理文档",
            **({"allowedTools": allowed_tools} if allowed_tools is not None else {}),
        }
        if command_type == "StartRun"
        else {}
    )
    return RuntimeCommand(
        command_id,
        command_type,  # type: ignore[arg-type]
        run_id,
        conversation_id,
        "2026-09-28T00:00:00+08:00",
        payload,
    )


class RuntimeCommandAdapterPhase1Test(unittest.IsolatedAsyncioTestCase):
    """覆盖 durable decision 与 WS logical run 的重启后续跑语义。"""

    async def test_reads_messages_from_durable_transcript_after_restart(self) -> None:
        """Browser history 只读投影来自 Transcript，不依赖旧 RunHandle。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager_a = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_a = RuntimeCommandAdapter(manager_a)

            decision = await adapter_a.handle(_command("cmd-history", "StartRun"))
            self.assertEqual(decision.status, "ACCEPTED")
            handle = manager_a._runs[adapter_a._manager_run_ids["R1"]]
            await asyncio.wait_for(handle.task, timeout=1)

            messages_before = adapter_a.get_run_messages("R1")
            self.assertIsNotNone(messages_before)
            self.assertEqual([message["role"] for message in messages_before], ["user", "assistant"])
            self.assertEqual(messages_before[0]["content"], "继续处理文档")
            self.assertEqual(messages_before[1]["content"], "暂停")

            # 进程重启后仍从相同 durable facts 重建相同 Browser-safe 历史。
            manager_b = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter_b = RuntimeCommandAdapter(manager_b)
            self.assertEqual(adapter_b.get_run_messages("R1"), messages_before)

    async def test_live_start_registers_workspace_run_for_control_commands(self) -> None:
        """Start Ack 后，同进程控制命令必须能由 WS runId 定位 internal run。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter = RuntimeCommandAdapter(manager)

            start = await adapter.handle(_command("cmd-start-live", "StartRun"))
            self.assertEqual("ACCEPTED", start.status)
            internal_run_id = adapter._manager_run_ids["R1"]
            self.assertEqual(
                internal_run_id,
                adapter._run_associations["R1"].latest_internal_run_id,
            )

            pause = await adapter.handle(_command("cmd-pause-live", "PauseRun"))
            self.assertEqual("ACCEPTED", pause.status)
            self.assertEqual("pause", manager._runs[internal_run_id].run_control.terminal_action)

            # Cancel 的优先级高于已经登记的 Pause，且仍命中同一个 internal run。
            cancel = await adapter.handle(_command("cmd-cancel-live", "CancelRun"))
            self.assertEqual("ACCEPTED", cancel.status)
            self.assertEqual("cancel", manager._runs[internal_run_id].run_control.terminal_action)
            await asyncio.wait_for(manager._runs[internal_run_id].task, timeout=1)

    async def test_live_start_registers_workspace_run_for_steering(self) -> None:
        """Steer 使用 WS runId 找到当前 RunControl，不把它当 internal run_id。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manager = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter = RuntimeCommandAdapter(manager)

            await adapter.handle(_command("cmd-start-steer", "StartRun"))
            internal_run_id = adapter._manager_run_ids["R1"]
            steer = RuntimeCommand(
                "cmd-steer-live",
                "SteerRun",
                "R1",
                "C1",
                "2026-09-28T00:00:01+08:00",
                {"instruction": "重点关注权限设计"},
            )

            decision = await adapter.handle(steer)

            self.assertEqual("ACCEPTED", decision.status)
            pending = manager._runs[internal_run_id].run_control.steering_mailbox.drain()
            self.assertEqual(["重点关注权限设计"], [query.content for query in pending])

    async def test_cancel_paused_run_commits_durable_effect_and_lifecycle(self) -> None:
        """暂停 execution 已结束后，Cancel 仍以 durable effect 收敛为 CANCELLED。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            callbacks: list[list[dict[str, object]]] = []
            manager = RunManager(
                HexgentConfig(),
                root,
                engine_factory=_ProbeEngineFactory(),
                lifecycle_callback=lambda _session_id, records: callbacks.append(records),
            )
            adapter = RuntimeCommandAdapter(manager)

            await adapter.handle(_command("cmd-start-paused-cancel", "StartRun"))
            internal_run_id = adapter._manager_run_ids["R1"]
            await adapter.handle(_command("cmd-pause-before-cancel", "PauseRun"))
            await asyncio.wait_for(manager._runs[internal_run_id].task, timeout=1)
            self.assertEqual("paused", manager._runs[internal_run_id].status)

            decision = await adapter.handle(
                _command("cmd-cancel-paused", "CancelRun")
            )

            self.assertEqual("ACCEPTED", decision.status)
            self.assertEqual("cancelled", manager._runs[internal_run_id].status)
            session_id = adapter._run_associations["R1"].session_id
            records = manager._require_session(session_id).engine.session.store.read_transcript()
            effects = [
                record.get("payload", {})
                for record in records
                if record.get("type") == "runtime_control_effect"
            ]
            self.assertTrue(any(
                effect.get("command_id") == "cmd-cancel-paused"
                and effect.get("effect") == "cancelled"
                for effect in effects
            ))
            self.assertTrue(callbacks)
            await asyncio.wait_for(manager._runs[internal_run_id].task, timeout=1)

    async def test_reload_hydrates_session_and_creates_new_internal_runs(self) -> None:
        """同一 WS Run 在重启后以新 internal run 继续，而不恢复旧 Handle。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            factory_a = _ProbeEngineFactory()
            manager_a = RunManager(HexgentConfig(), root, engine_factory=factory_a)
            adapter_a = RuntimeCommandAdapter(manager_a)

            start = await adapter_a.handle(
                _command("cmd-start", "StartRun", allowed_tools=["search_workspace"])
            )
            self.assertEqual(start.status, "ACCEPTED")
            first_handle = manager_a._runs[adapter_a._manager_run_ids["R1"]]
            await asyncio.wait_for(first_handle.task, timeout=1)
            session_id = first_handle.session_id
            internal_a = self._internal_run_ids(root, session_id)[-1]

            # 1. 进程重启后仅依赖 Transcript；旧 RunHandle 不传入新 Manager。
            factory_b = _ProbeEngineFactory()
            manager_b = RunManager(HexgentConfig(), root, engine_factory=factory_b)
            adapter_b = RuntimeCommandAdapter(manager_b)
            self.assertEqual(adapter_b._run_associations["R1"].session_id, session_id)
            self.assertEqual(adapter_b._run_associations["R1"].latest_internal_run_id, internal_a)
            self.assertEqual(manager_b._runs, {})

            # 2. 相同 commandId 恢复原 Decision，且不会新建 internal run。
            duplicate = await adapter_b.handle(
                _command("cmd-start", "StartRun", allowed_tools=["search_workspace"])
            )
            self.assertEqual(duplicate, start)
            self.assertEqual(self._internal_run_ids(root, session_id), [internal_a])

            # 3. Continue hydrate 新 Engine，使用 RunRequest.continue_run 产生 internal B。
            continued = await adapter_b.handle(_command("cmd-continue", "ContinueRun"))
            self.assertEqual(continued.status, "ACCEPTED")
            second_handle = manager_b._runs[adapter_b._manager_run_ids["R1"]]
            await asyncio.wait_for(second_handle.task, timeout=1)
            self.assertEqual(factory_b.restored_engines[-1].requests[-1].intent, "continue")
            self.assertEqual(
                factory_b.restored_engines[-1].tool_context.workspace_capability_scope,
                frozenset({"search_workspace"}),
            )
            internal_b = self._internal_run_ids(root, session_id)[-1]
            self.assertNotEqual(internal_a, internal_b)

            # 4. 再次重启后，Resume 同样只恢复 Session 并创建 internal C。
            factory_c = _ProbeEngineFactory()
            manager_c = RunManager(HexgentConfig(), root, engine_factory=factory_c)
            adapter_c = RuntimeCommandAdapter(manager_c)
            self.assertEqual(adapter_c._run_associations["R1"].latest_internal_run_id, internal_b)
            resumed = await adapter_c.handle(_command("cmd-resume", "ResumeRun"))
            self.assertEqual(resumed.status, "ACCEPTED")
            third_handle = manager_c._runs[adapter_c._manager_run_ids["R1"]]
            await asyncio.wait_for(third_handle.task, timeout=1)
            internal_c = self._internal_run_ids(root, session_id)[-1]
            self.assertNotEqual(internal_b, internal_c)
            self.assertEqual(factory_c.restored_engines[-1].requests[-1].intent, "continue")
            self.assertEqual(
                factory_c.restored_engines[-1].tool_context.workspace_capability_scope,
                frozenset({"search_workspace"}),
            )
            self.assertEqual(self._workspace_runs(root, session_id), ["R1", "R1", "R1"])

            wrong_conversation = await adapter_c.handle(
                _command("cmd-wrong-conversation", "ContinueRun", conversation_id="C2"),
            )
            self.assertEqual(wrong_conversation.status, "REJECTED")
            self.assertEqual(wrong_conversation.reason_code, "CONVERSATION_MISMATCH")

            adapter_d = RuntimeCommandAdapter(
                RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory()),
            )
            self.assertEqual(
                await adapter_d.handle(
                    _command("cmd-wrong-conversation", "ContinueRun", conversation_id="C2"),
                ),
                wrong_conversation,
            )

    async def test_rejects_existing_session_without_continuable_checkpoint(self) -> None:
        """Session 存在但没有合法 Checkpoint 时，不得退化成新的 submit。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            session.initialize_runtime_session("session_without_checkpoint")
            context = ContextManager()
            query = context.append_user_query("已经完成的任务")
            session.record_user_query(query)
            session.bind_runtime_run("R1", "C1")
            session.start_run(limits_enabled=False, plan=None)
            session.finish_run(
                status="completed",
                stop_reason="completed",
                elapsed_seconds=0.01,
                output_text="完成",
                plan=None,
                context_manager=context,
                loop_state=LoopState(
                    context,
                    ToolExecutionContext(InMemoryPlanStore(), root),
                ),
            )

            manager = RunManager(HexgentConfig(), root, engine_factory=_ProbeEngineFactory())
            adapter = RuntimeCommandAdapter(manager)
            decision = await adapter.handle(_command("cmd-no-checkpoint", "ContinueRun"))

            self.assertEqual(decision.status, "REJECTED")
            self.assertEqual(decision.reason_code, "RUNTIME_STATE_NOT_CONTINUABLE")
            self.assertEqual(manager._runs, {})

    @staticmethod
    def _internal_run_ids(root: Path, session_id: str) -> list[str]:
        """读取 Transcript 中的 internal run 标识，验证它们始终属于同一 WS Run。"""
        records = (root / ".hexgent" / "sessions" / session_id / "transcript.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
        return [
            json.loads(record)["payload"]["run_id"]
            for record in records
            if json.loads(record)["type"] == "agent_run_started"
        ]

    @staticmethod
    def _workspace_runs(root: Path, session_id: str) -> list[str]:
        """读取每个 internal run 写入的稳定 WS logical run 身份。"""
        records = (root / ".hexgent" / "sessions" / session_id / "transcript.jsonl").read_text(
            encoding="utf-8",
        ).splitlines()
        return [
            json.loads(record)["payload"]["workspace_run_id"]
            for record in records
            if json.loads(record)["type"] == "agent_run_started"
        ]
