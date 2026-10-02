"""Web Adapter 的 Session、Run、SSE 与控制协议测试。"""

from __future__ import annotations

import time
import unittest
from collections.abc import Generator
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from hexgent.adapters.web.app import create_app
from hexgent.adapters.web.event_serializer import agent_event_payload, serialize_agent_event
from hexgent.adapters.web.run_manager import RunManager
from hexgent.adapters.web.session_projection import transcript_messages
from hexgent.adapters.web.settings import WebSettings
from hexgent.config import HexgentConfig
from hexgent.events import AgentEvent, AgentLoopResult, RunFinishedEvent, TextDeltaEvent
from hexgent.plan import InMemoryPlanStore, Plan, PlanStep
from hexgent.run_control import RunControl
from hexgent.session import Session, SessionSettings
from hexgent.tool.tool_permission_pipeline import UserConfirmationResult


class FakeAgentEngine:
    """可控地模拟同步 AgentEngine 事件流。"""

    def __init__(self, config: HexgentConfig, workdir: Path) -> None:
        self.workdir = workdir
        self.session = Session(settings=SessionSettings(config.permission.default_mode))
        self.tool_context = SimpleNamespace(plan_store=InMemoryPlanStore())
        self.started = Event()
        self.release = Event()
        self.wait_for_release = False
        self.request_permission = False
        self.last_control: RunControl | None = None
        self.permission_result: UserConfirmationResult | None = None

    def run(
        self,
        query: str,
        *,
        user_confirmation: Any,
        run_control: RunControl,
        **_: Any,
    ) -> Generator[AgentEvent, None, None]:
        """按测试开关等待、请求权限并生成稳定终态。"""
        self.last_control = run_control
        self.started.set()
        if self.request_permission:
            self.permission_result = user_confirmation("write_file", "是否写入文件？")
        while (
            self.wait_for_release
            and not self.release.wait(timeout=0.01)
            and not run_control.interrupt_controller.interrupted
        ):
            continue
        yield TextDeltaEvent(f"收到：{query}")
        action = run_control.terminal_action
        status = (
            "cancelled"
            if action == "cancel"
            else "interrupted"
            if action == "pause"
            else "completed"
        )
        yield RunFinishedEvent(AgentLoopResult(f"收到：{query}", 0.01, status=status))

    def cancel_pending_task(self, _reason: str = "用户取消暂停任务") -> None:
        """模拟 Core 终结暂停后保留的 Plan。"""
        self.tool_context.plan_store.clear_plan()


class FakeEngineFactory:
    """记录每个 Web Session 获得的 FakeAgentEngine。"""

    def __init__(self) -> None:
        self.engines: list[FakeAgentEngine] = []

    def __call__(self, config: HexgentConfig, workdir: Path) -> FakeAgentEngine:
        engine = FakeAgentEngine(config, workdir)
        self.engines.append(engine)
        return engine


class WebAdapterTest(unittest.TestCase):
    """验证网络层只编排 Core，不复制 Agent 控制逻辑。"""

    def setUp(self) -> None:
        self.factory = FakeEngineFactory()
        self.manager = RunManager(
            HexgentConfig(),
            Path.cwd(),
            engine_factory=self.factory,
        )
        self.client_context = TestClient(create_app(run_manager=self.manager))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def _create_session(self) -> str:
        response = self.client.post(
            "/api/sessions",
            json={"permission_mode": "default"},
        )
        self.assertEqual(response.status_code, 201)
        return response.json()["session_id"]

    def _wait_for_terminal(self, run_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/runs/{run_id}")
            body = response.json()
            if body["status"] not in {"pending", "running"}:
                return body
            time.sleep(0.01)
        self.fail("测试 Run 未在期限内结束")

    def test_text_events_preserve_true_deltas_and_stable_event_identity(self) -> None:
        """Browser contract 不把文本快照伪装成 delta，且每个事件可幂等识别。"""
        events = [
            serialize_agent_event(
                TextDeltaEvent(delta),
                run_id="run-001",
                stream_id="stream-001",
                sequence=index,
                response_id="response-001",
            )
            for index, delta in enumerate(["你", "好", "！"], start=1)
        ]

        self.assertEqual([event["payload"]["delta"] for event in events if event], ["你", "好", "！"])
        self.assertEqual(
            [event["event_id"] for event in events if event],
            ["stream-001:1", "stream-001:2", "stream-001:3"],
        )

    def test_run_events_are_replayed_in_order_and_sse_closes_on_terminal(self) -> None:
        """SSE 只回放 Phase 3 允许的非权威事件，并在终态后关闭。"""
        session_id = self._create_session()
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "你好"},
        )
        self.assertEqual(started.status_code, 202)
        run_id = started.json()["run_id"]
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "completed")

        response = self.client.get(f"/api/runs/{run_id}/events")

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: text_delta", response.text)
        self.assertIn("id: 1", response.text)
        self.assertIn('"stream_id":"stream_', response.text)
        self.assertIn('"event_id":"stream_', response.text)
        self.assertIn('"responseId":"response_', response.text)
        self.assertNotIn("run_finished", response.text)

    def test_stream_hides_core_events_outside_phase3_allowlist(self) -> None:
        """SSE 不应暴露终态、批次和内部控制事件。"""
        session_id = self._create_session()
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "你好"},
        )
        run_id = started.json()["run_id"]
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "completed")

        response = self.client.get(f"/api/runs/{run_id}/events")

        self.assertNotIn("run_finished", response.text)
        self.assertNotIn("tool_execution_completed", response.text)
        self.assertNotIn("loop_transition", response.text)

    def test_session_rejects_second_active_run(self) -> None:
        """同一 Session 的第二个并发 Run 应获得稳定 409。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.wait_for_release = True
        first = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "第一个任务"},
        )
        self.assertTrue(engine.started.wait(timeout=1))

        second = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "第二个任务"},
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error"]["code"], "run_already_active")
        engine.release.set()
        self._wait_for_terminal(first.json()["run_id"])

    def test_permission_request_can_be_resolved_over_http(self) -> None:
        """工具线程应等待 Web 审批，并在 allow 后继续完成。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.request_permission = True
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "写文件"},
        )
        run_id = started.json()["run_id"]

        deadline = time.monotonic() + 2
        request_id = None
        while time.monotonic() < deadline and request_id is None:
            events, _ = self.client.app.state.run_manager._runs[run_id].events, False
            for event in events:
                if event["type"] == "permission_requested":
                    request_id = event["payload"]["request_id"]
                    break
            time.sleep(0.01)
        self.assertIsNotNone(request_id)
        self.assertEqual(
            self.client.get(f"/api/runs/{run_id}").json()["status"],
            "running",
        )
        permission_event = next(
            event
            for event in self.client.app.state.run_manager._runs[run_id].events
            if event["type"] == "permission_requested"
        )
        self.assertEqual(permission_event["payload"]["reason"], "是否写入文件？")
        self.assertEqual(permission_event["payload"]["permission_mode"], "default")

        response = self.client.post(
            f"/api/runs/{run_id}/permissions/{request_id}",
            json={"decision": "allow", "response_id": "response-allow-1"},
        )

        self.assertEqual(response.status_code, 202)
        duplicate = self.client.post(
            f"/api/runs/{run_id}/permissions/{request_id}",
            json={"decision": "allow", "response_id": "response-allow-1"},
        )
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "completed")
        self.assertEqual(engine.permission_result, "allow")

    def test_permission_resolution_rejects_conflicting_or_lost_pending_request(self) -> None:
        """同一决定可幂等；冲突决定与已丢失的 PendingPermission 必须拒绝。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.request_permission = True
        started = self.client.post(f"/api/sessions/{session_id}/runs", json={"query": "写文件"})
        run_id = started.json()["run_id"]

        deadline = time.monotonic() + 2
        request_id = None
        while time.monotonic() < deadline and request_id is None:
            for event in self.client.app.state.run_manager._runs[run_id].events:
                if event["type"] == "permission_requested":
                    request_id = event["payload"]["request_id"]
                    break
            time.sleep(0.01)
        self.assertIsNotNone(request_id)
        accepted = self.client.post(
            f"/api/runs/{run_id}/permissions/{request_id}",
            json={"decision": "deny", "response_id": "response-deny-1"},
        )
        self.assertEqual(accepted.status_code, 202)
        conflict = self.client.post(
            f"/api/runs/{run_id}/permissions/{request_id}",
            json={"decision": "allow", "response_id": "response-deny-1"},
        )
        self.assertEqual(conflict.status_code, 409)
        missing = self.client.post(
            f"/api/runs/{run_id}/permissions/missing-request",
            json={"decision": "deny", "response_id": "response-missing"},
        )
        self.assertEqual(missing.status_code, 404)
        self._wait_for_terminal(run_id)

    def test_steering_is_forwarded_to_core_run_control_fifo(self) -> None:
        """Web Steering 不应自行改写 Context，而应进入 Core FIFO。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.wait_for_release = True
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "原任务"},
        )
        self.assertTrue(engine.started.wait(timeout=1))
        run_id = started.json()["run_id"]

        response = self.client.post(
            f"/api/runs/{run_id}/steer",
            json={"query": "先检查测试"},
        )

        self.assertEqual(response.status_code, 202)
        assert engine.last_control is not None
        queries = engine.last_control.steering_mailbox.drain()
        self.assertEqual([item.content for item in queries], ["先检查测试"])
        engine.release.set()
        self._wait_for_terminal(run_id)

    def test_session_response_contains_refresh_projection(self) -> None:
        """Session 响应应包含前端刷新所需的稳定字段。"""
        session_id = self._create_session()

        response = self.client.get(f"/api/sessions/{session_id}")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["workdir"], str(Path.cwd().resolve()))
        self.assertEqual(body["recent_run_ids"], [])
        self.assertIsNone(body["plan"])
        self.assertEqual(body["messages"], [])

    def test_pause_callback_does_not_block_http_event_loop(self) -> None:
        """阻塞式流关闭回调不应卡住 FastAPI 的事件循环。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.wait_for_release = True
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "长任务"},
        )
        self.assertTrue(engine.started.wait(timeout=1))
        run_id = started.json()["run_id"]

        callback_started = Event()
        release_callback = Event()

        def blocking_close() -> None:
            callback_started.set()
            release_callback.wait(timeout=1)

        assert engine.last_control is not None
        engine.last_control.interrupt_controller.add_callback(blocking_close)
        import threading

        response_holder: list[Any] = []
        request_thread = threading.Thread(
            target=lambda: response_holder.append(
                self.client.post(f"/api/runs/{run_id}/pause")
            )
        )
        request_thread.start()
        self.assertTrue(callback_started.wait(timeout=1))

        # Pause 尚在等待 close，GET 仍应可以获得事件循环响应。
        status_response = self.client.get(f"/api/runs/{run_id}")
        self.assertEqual(status_response.status_code, 200)
        release_callback.set()
        request_thread.join(timeout=1)
        self.assertEqual(response_holder[0].status_code, 202)
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "paused")

    def test_new_run_is_immediately_controllable_and_pause_reaches_core(self) -> None:
        """创建接口返回后应立即可暂停，不能因 pending/running 不同步而静默失效。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.wait_for_release = True
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "长任务"},
        )
        self.assertTrue(engine.started.wait(timeout=1))
        run_id = started.json()["run_id"]

        paused = self.client.post(f"/api/runs/{run_id}/pause")

        self.assertEqual(started.json()["status"], "running")
        self.assertEqual(paused.status_code, 202)
        assert engine.last_control is not None
        self.assertEqual(engine.last_control.terminal_action, "pause")
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "paused")

    def test_paused_run_can_cancel_retained_plan_before_new_query(self) -> None:
        """暂停后提交新目标前，应能终结旧 Plan 且不伪装为活跃 Run Cancel。"""
        session_id = self._create_session()
        engine = self.factory.engines[0]
        engine.tool_context.plan_store.set_plan(
            Plan(
                "旧任务",
                "in_progress",
                [PlanStep("继续旧任务", "正在继续旧任务", "in_progress", "step-1")],
            )
        )
        engine.wait_for_release = True
        started = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "旧任务"},
        )
        self.assertTrue(engine.started.wait(timeout=1))
        run_id = started.json()["run_id"]
        self.assertEqual(self.client.post(f"/api/runs/{run_id}/pause").status_code, 202)
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "paused")

        cancelled = self.client.post(f"/api/runs/{run_id}/cancel")
        restarted = self.client.post(
            f"/api/sessions/{session_id}/runs",
            json={"query": "新任务"},
        )

        self.assertEqual(cancelled.status_code, 202)
        self.assertIsNone(engine.tool_context.plan_store.get_plan())
        self.assertEqual(restarted.status_code, 202)
        engine.release.set()
        self._wait_for_terminal(restarted.json()["run_id"])

    def test_cors_only_allows_configured_origin(self) -> None:
        """浏览器跨域预检只能命中显式 Origin 白名单。"""
        app = create_app(
            run_manager=self.manager,
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

    def test_transcript_projects_only_visible_conversation(self) -> None:
        """Session 刷新投影应排除工具协议等内部记录。"""
        records = [
            {
                "sequence": 1,
                "type": "context_user_query_appended",
                "created_at": "2026-09-01T10:00:00+08:00",
                "payload": {"content": "你好"},
            },
            {
                "sequence": 2,
                "type": "context_messages_appended",
                "created_at": "2026-09-01T10:00:01+08:00",
                "payload": {"messages": [{
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "你好！"}],
                }], "completes_round": True, "source": "model_response"},
            },
            {
                "sequence": 3,
                "type": "context_messages_appended",
                "created_at": "2026-09-01T10:00:02+08:00",
                "payload": {"messages": [
                    {"type": "function_call_output", "output": "内部结果"}
                ], "completes_round": True, "source": "tool_results"},
            },
        ]
        self.assertEqual(
            [(item["role"], item["content"]) for item in transcript_messages(records)],
            [("user", "你好"), ("assistant", "你好！")],
        )

    def test_transcript_hides_resume_query_and_persists_steering(self) -> None:
        """内部续跑指令不可见，Steering 应作为用户消息在刷新后恢复。"""
        records = [
            {
                "sequence": 1,
                "type": "web_hidden_query",
                "created_at": "2026-09-01T10:00:00+08:00",
                "payload": {"content": "继续"},
            },
            {
                "sequence": 2,
                "type": "context_user_query_appended",
                "created_at": "2026-09-01T10:00:01+08:00",
                "payload": {"content": "继续"},
            },
            {
                "sequence": 3,
                "type": "web_steering_submitted",
                "created_at": "2026-09-01T10:00:02+08:00",
                "payload": {"content": "改为只读检查"},
            },
            {
                "sequence": 4,
                "type": "context_user_query_appended",
                "created_at": "2026-09-01T10:00:03+08:00",
                "payload": {
                    "query_sequence": 2,
                    "content": "改为只读检查",
                    "source": "steering",
                },
            },
        ]
        self.assertEqual(
            [(item["role"], item["content"]) for item in transcript_messages(records)],
            [("user", "改为只读检查")],
        )

    def test_transcript_uses_finished_output_as_refresh_fallback(self) -> None:
        """缺少 Assistant message 时仍可由 Run 终态恢复最终回答。"""
        records = [
            {
                "sequence": 1,
                "type": "context_user_query_appended",
                "created_at": "2026-09-01T10:00:00+08:00",
                "payload": {"content": "检查项目"},
            },
            {
                "sequence": 2,
                "type": "agent_run_finished",
                "created_at": "2026-09-01T10:00:01+08:00",
                "payload": {"output_text": "检查完成"},
            },
        ]
        self.assertEqual(
            [(item["role"], item["content"]) for item in transcript_messages(records)],
            [("user", "检查项目"), ("assistant", "检查完成")],
        )

    def test_unknown_agent_event_is_rejected(self) -> None:
        """未知领域事件不得通过通用序列化泄漏内部字段。"""
        with self.assertRaises(TypeError):
            agent_event_payload(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
