"""Phase 4：真实 Manager/Core 与终端输入所有权验证，不调用真实模型。"""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from threading import Event
from threading import Thread
from types import SimpleNamespace
import os
import pty

from hexgent.agent_engine import AgentEngine
from hexgent.config import HexgentConfig
from hexgent.adapters.web.run_manager import RunManager
from hexgent.adapters.web.session_projection import transcript_messages
from hexgent.adapters.cli.interactive_run import submit_control
from hexgent.adapters.cli.interactive_run import run_interactive
from hexgent.adapters.cli.terminal_io import TerminalIO
from hexgent.run_control import RunControl
from tests.test_interrupting_steer import Client, GatedStream, completed


class AdapterSteeringTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_identity_and_conflict_at_adapter_boundary(self):
        with TemporaryDirectory() as directory:
            manager = RunManager(HexgentConfig(), Path(directory))
            session = await manager.create_session("default")
            from hexgent.adapters.web.run_manager import RunHandle
            from hexgent.adapters.web.errors import WebAdapterError
            handle = RunHandle("manual", session.session_id, "任务", True, RunControl(), status="running")
            manager._runs["manual"] = handle
            await manager.steer("manual", "唯一原文", mode="boundary", command_id="stable")
            await manager.steer("manual", "唯一原文", mode="boundary", command_id="stable")
            with self.assertRaises(WebAdapterError):
                await manager.steer("manual", "另一原文", mode="boundary", command_id="stable")
            self.assertEqual(len(handle.run_control.drain_steering()), 1)
            self.assertEqual(len(manager._sessions[session.session_id].engine.session.steering_intents()), 1)

    async def test_real_manager_preempts_core_and_persists_identity(self):
        with TemporaryDirectory() as directory:
            stream = GatedStream()
            client = Client([stream, [completed()]])
            class Engine(AgentEngine):
                def run(self, *args, **kwargs):
                    return super().run(*args, client=client, **kwargs)
            manager = RunManager(HexgentConfig(), Path(directory), engine_factory=Engine)
            with patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
                try:
                    session = await manager.create_session("default")
                    run = await manager.start_run(session.session_id, "原始任务")
                    self.assertTrue(await asyncio.to_thread(stream.waiting.wait, 3))
                    await manager.steer(run.run_id, "先检查风险", command_id="adapter-1")
                    await asyncio.wait_for(manager._runs[run.run_id].task, 5)
                    self.assertTrue(stream.closed)
                    self.assertEqual(len(client.calls), 2)
                    self.assertIn("先检查风险", str(client.calls[1]["input"]))
                    core = manager._sessions[session.session_id].engine.session
                    self.assertEqual(len(core.steering_intents()), 1)
                    self.assertEqual(core.steering_intents()[0]["command_id"], "adapter-1")
                    self.assertEqual(len(core.steering_context_mutations("adapter-1")), 1)
                    kinds = [e["type"] for e in manager._runs[run.run_id].events]
                    self.assertIn("steering_received", kinds)
                    self.assertIn("steering_applied", kinds)
                finally:
                    stream.released.set()
                    await manager.shutdown()

    async def test_slow_stream_close_does_not_block_event_loop(self):
        with TemporaryDirectory() as directory:
            manager = RunManager(HexgentConfig(), Path(directory))
            session = await manager.create_session("default")
            from hexgent.adapters.web.run_manager import RunHandle
            control = RunControl()
            handle = RunHandle("manual", session.session_id, "任务", True, control, status="running")
            manager._runs["manual"] = handle
            entered, release = Event(), Event()
            with control.model_request() as request:
                request.add_callback(lambda: (entered.set(), release.wait(3)))
                operation = asyncio.create_task(manager.steer("manual", "新要求", command_id="slow"))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    # 若关闭流在事件循环线程执行，这个协程无法在释放前运行。
                    self.assertFalse(operation.done())
                finally:
                    release.set()
                    await operation


class CliSteeringTests(unittest.TestCase):
    def test_tty_steer_during_permission_never_authorizes_old_write(self):
        with TemporaryDirectory() as directory:
            import json
            pending = Event()
            tool_call = SimpleNamespace(type="function_call", call_id="old-write", name="write_file",
                arguments=json.dumps({"path": "forbidden.txt", "content": "old"}))
            client = Client([[completed(output=[tool_call])], [completed()]])
            class Engine(AgentEngine):
                def run(self, *args, **kwargs):
                    callback = kwargs.pop("user_confirmation")
                    def confirm(*values):
                        pending.set()
                        return callback(*values)
                    return super().run(*args, client=client, user_confirmation=confirm, **kwargs)
            engine = Engine(HexgentConfig(), Path(directory))
            master, slave = pty.openpty()
            reader = os.fdopen(slave, "r")
            def send():
                if pending.wait(3):
                    os.write(master, "/steer 取消旧写入，先分析\n".encode())
            writer = Thread(target=send)
            writer.start()
            try:
                with patch("sys.stdin", reader), patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
                    from hexgent.agent_engine import RunRequest
                    run_interactive(TerminalIO(), engine, RunRequest.submit("修改资料"), RunControl(),
                        SimpleNamespace(render=lambda _: None))
                self.assertFalse((Path(directory) / "forbidden.txt").exists())
                self.assertIn("取消旧写入，先分析", str(client.calls[1]["input"]))
                records = engine.session.store.read_transcript()
                self.assertFalse(any(r["type"] == "tool_execution_dispatched" for r in records))
            finally:
                writer.join(3)
                reader.close()
                os.close(master)

    def test_real_tty_accepts_new_query_while_model_is_running(self):
        with TemporaryDirectory() as directory:
            stream = GatedStream()
            client = Client([stream, [completed()]])
            class Engine(AgentEngine):
                def run(self, *args, **kwargs):
                    return super().run(*args, client=client, **kwargs)
            engine = Engine(HexgentConfig(), Path(directory))
            master, slave = pty.openpty()
            reader = os.fdopen(slave, "r")
            terminal = TerminalIO()
            def type_query():
                if stream.waiting.wait(3):
                    os.write(master, "先核验风险\n\n".encode())
            writer = Thread(target=type_query)
            writer.start()
            try:
                with patch("sys.stdin", reader), patch("hexgent.loop.agent_turn.get_model", return_value="offline"):
                    from hexgent.agent_engine import RunRequest
                    run_interactive(terminal, engine, RunRequest.submit("原始任务"), RunControl(),
                        SimpleNamespace(render=lambda _: None))
                self.assertTrue(stream.closed)
                self.assertIn("先核验风险", str(client.calls[1]["input"]))
                self.assertEqual(len(engine.session.steering_intents()), 1)
            finally:
                stream.released.set()
                writer.join(3)
                reader.close()
                os.close(master)

    def test_explicit_and_default_modes_preserve_text(self):
        control = RunControl()
        submit_control(control, "用户新要求")
        submit_control(control, "/queue 后续要求")
        submit_control(control, "/steer 现在调整")
        queries = control.drain_steering()
        self.assertEqual([q.mode for q in queries], ["interrupt", "boundary", "interrupt"])
        self.assertEqual([q.content for q in queries], ["用户新要求", "后续要求", "现在调整"])

    def test_management_commands_do_not_become_user_query(self):
        control = RunControl()
        with self.assertRaises(ValueError):
            submit_control(control, "/pin 不能偷偷管理约束")
        self.assertFalse(control.steering_mailbox.has_pending)
        submit_control(control, "/pause")
        self.assertEqual(control.terminal_action, "pause")
        submit_control(control, "/cancel")
        self.assertEqual(control.terminal_action, "cancel")

    def test_projection_uses_command_identity_not_content(self):
        def record(seq, kind, payload):
            return {"sequence": seq, "type": kind, "payload": payload, "created_at": "now"}
        records = [record(1, "runtime_control_effect", {"effect": "steering_intent", "command_id": "a", "instruction": "同文"}),
            record(2, "runtime_control_effect", {"effect": "steering_intent", "command_id": "b", "instruction": "同文"}),
            record(3, "context_user_query_appended", {"source": "steering", "command_id": "a", "content": "同文"})]
        self.assertEqual(len(transcript_messages(records)), 2)
