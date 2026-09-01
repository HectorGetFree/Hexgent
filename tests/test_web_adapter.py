"""Web Adapter 的 Session、Run、SSE 与控制协议测试。"""

from __future__ import annotations

import time
import unittest
from collections.abc import Generator
from pathlib import Path
from threading import Event
from typing import Any

from fastapi.testclient import TestClient

from hexgent.adapters.web.app import create_app
from hexgent.adapters.web.run_manager import RunManager
from hexgent.config import HexgentConfig
from hexgent.events import AgentEvent, AgentLoopResult, RunFinishedEvent, TextDeltaEvent
from hexgent.run_control import RunControl
from hexgent.tool.tool_permission_pipeline import UserConfirmationResult


class FakeAgentEngine:
    """可控地模拟同步 AgentEngine 事件流。"""

    def __init__(self, _config: HexgentConfig, _workdir: Path) -> None:
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
        if self.wait_for_release:
            self.release.wait(timeout=2)
        yield TextDeltaEvent(f"收到：{query}")
        yield RunFinishedEvent(AgentLoopResult(f"收到：{query}", 0.01))


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

    def test_run_events_are_replayed_in_order_and_sse_closes_on_terminal(self) -> None:
        """SSE 应按 sequence 回放文本和唯一终态事件。"""
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
        self.assertIn("event: run_finished", response.text)
        self.assertIn("id: 1", response.text)
        self.assertIn("id: 2", response.text)

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

        response = self.client.post(
            f"/api/runs/{run_id}/permissions/{request_id}",
            json={"decision": "allow"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(self._wait_for_terminal(run_id)["status"], "completed")
        self.assertEqual(engine.permission_result, "allow")

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


if __name__ == "__main__":
    unittest.main()
