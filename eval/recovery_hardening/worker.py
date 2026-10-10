"""仅由离线 runner 调用的真实进程 crash/restore 执行端。"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eval.modules_v1.runner import encode
from eval.phase5.scenarios import BenchmarkResponses, _base_config
from eval.support.agent_contracts import text_response, tool_call
from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.adapters.web.run_manager import RunManager
from hexgent.session.session_store import SessionStore
from hexgent.tool.tool_registry import TOOL_REGISTRY, RegisteredTool
from hexgent.tool.tool_validation import TOOL_SCHEMAS
from hexgent.tool.tool_result import ToolHandlerResult


CALL_ID = "hardening-call"
TOOL = "eval_recovery_audit"
CRASH_EXIT = 73
SCENARIOS = (
    "finished_before_context", "safe_started_before_dispatch", "handler_before_effect",
    "handler_partial_effect", "committed_before_finished", "permission_denied",
    "permission_pending", "safe_checkpoint",
)


def save(path, value):
    """观测端写入独立 artifact，并同步以供父进程检查。"""
    with path.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, default=encode, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def ledger(root, event):
    """真实 handler 的可独立审计执行轨迹，不写入 Runtime Transcript。"""
    with (root / "effects.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, "call_id": CALL_ID, "pid": os.getpid()}) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def business_write(root, value):
    """固定业务状态写入，不执行模型提供的任意命令或路径。"""
    with (root / "business.txt").open("w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def crash(root, point, store):
    """无 finally/unwind 地结束整个子进程，而不是抛出异常替代重启。"""
    save(root / "crash-boundary.json", {"pid": os.getpid(), "point": point, "session_id": store.session_id})
    os._exit(CRASH_EXIT)


def register(root, scenario, store_holder):
    """仅替换 Eval registry，新工具仍经过正式参数校验与权限管线。"""
    safe = scenario == "safe_started_before_dispatch"

    def handler():
        ledger(root, "handler_entered")
        if scenario == "handler_before_effect":
            crash(root, "handler_entered_before_side_effect", store_holder[0])
        if not safe:
            ledger(root, "side_effect_started")
            business_write(root, "PARTIAL")
            if scenario == "handler_partial_effect":
                crash(root, "partial_business_state_before_commit", store_holder[0])
            business_write(root, "COMMITTED")
            ledger(root, "side_effect_committed")
        ledger(root, "handler_returned")
        return ToolHandlerResult.success("AUDIT_RESULT:read-only" if safe else "AUDIT_RESULT:COMMITTED")

    return (patch.dict(TOOL_REGISTRY, {TOOL: RegisteredTool(handler,
        permission_capability="workspace_read" if safe else "workspace_write",
        interrupt_behavior="cancel" if safe else "block")}),
        patch.dict(TOOL_SCHEMAS, {TOOL: {"type": "object", "properties": {}, "additionalProperties": False}}))


def child_a(root, scenario):
    """Production 调用链到达精确 crash boundary 后退出进程 A。"""
    original = SessionStore.append_transcript
    holder = [None]

    def append(store, kind, payload):
        holder[0] = store
        if scenario == "committed_before_finished" and kind == "tool_execution_finished":
            crash(root, "handler_returned_before_finished", store)
        if scenario == "finished_before_context" and kind == "context_messages_appended" and payload.get("source") == "tool_results":
            crash(root, "finished_before_durable_context_output", store)
        result = original(store, kind, payload)
        points = {
            "safe_started_before_dispatch": "tool_execution_started",
            "permission_denied": "permission_resolved",
            "permission_pending": "permission_requested",
            "safe_checkpoint": "checkpoint_committed",
        }
        if points.get(scenario) == kind:
            crash(root, f"after_{kind}", store)
        return result

    client = BenchmarkResponses([text_response(output=[tool_call(CALL_ID, TOOL, "{}")]), text_response("不应到达")])
    registry, schema = register(root, scenario, holder)
    with registry, schema, patch.object(SessionStore, "append_transcript", append), patch("httpx.Client.send", side_effect=AssertionError("禁止网络")):
        if scenario == "permission_pending":
            async def pending():
                manager = RunManager(_base_config(), root)
                session = await manager.create_session("default")
                await manager.start_run(session.session_id, "执行受控恢复工具")
                await asyncio.wait_for(manager._runs[next(iter(manager._runs))].task, timeout=15)
            with patch("hexgent.loop.agent_loop.get_client", return_value=SimpleNamespace(responses=client)):
                asyncio.run(pending())
        else:
            engine = AgentEngine.create(_base_config(), root)
            list(engine.run("执行受控恢复工具", client=client,
                user_confirmation=lambda *_: "deny" if scenario == "permission_denied" else "allow"))
    raise AssertionError("未到达目标 crash boundary")


def child_b(root, scenario):
    """仅使用磁盘 Session 重建 Engine，分别检查 continue 与新 Query follow-up。"""
    boundary = json.loads((root / "crash-boundary.json").read_text())
    holder = [None]
    registry, schema = register(root, scenario, holder)
    with registry, schema, patch("httpx.Client.send", side_effect=AssertionError("禁止网络")):
        engine = AgentEngine.restore(_base_config(), root, boundary["session_id"])
        holder[0] = engine.session.store
        first_records = engine.session.store.read_transcript()
        restored_messages = engine.context_manager.model_messages()
        restored = AgentEngine.restore(_base_config(), root, boundary["session_id"])
        holder[0] = restored.session.store
        probe = BenchmarkResponses([text_response("RECOVERY_OBSERVED")])

        def no_confirmation(*_):
            raise AssertionError("恢复阶段不应发生新权限确认")

        blocked = None
        before_continue = restored.has_continuable_run
        try:
            events = list(restored.run(RunRequest.continue_run(), client=probe, user_confirmation=no_confirmation))
            mode = "SAME_RUN_CONTINUE"
        except RuntimeError as failure:
            if str(failure) != "当前 Session 没有可继续的 Agent Run":
                raise
            blocked = str(failure)
            if probe.calls:
                raise AssertionError("continue 拒绝之前不应发送模型请求")
            events = list(restored.run(RunRequest.submit("核对恢复结果，不重试旧调用，继续给出结论"),
                client=probe, user_confirmation=no_confirmation))
            mode = "NEW_QUERY_SESSION_FOLLOWUP"
        save(root / "restored-observation.json", {
            "pid": os.getpid(), "session_id": restored.session.store.session_id,
            "has_continuable_run": before_continue, "continue_blocked_reason": blocked,
            "execution_mode": mode, "terminal_status": events[-1].result.status,
            "terminal_output": events[-1].result.output_text,
            "first_restored_messages": restored_messages, "first_restore_records": first_records,
            "model_requests": probe.calls, "final_records": restored.session.store.read_transcript(),
            "final_messages": restored.context_manager.model_messages(),
        })


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("crash", "restore"))
    parser.add_argument("scenario", choices=SCENARIOS)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    (child_a if args.phase == "crash" else child_b)(args.root, args.scenario)
