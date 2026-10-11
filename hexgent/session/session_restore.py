"""从可重放 Transcript 恢复 Hexgent 会话核心状态。"""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from ..context import ContextManager
from ..memory import ContextRecordStore
from ..plan import InMemoryPlanStore, Plan, PlanStep
from ..tool.tool_context import ToolExecutionContext
from ..tool.tool_permission_pipeline import PermissionMode
from ..loop.loop_state import RunStateSnapshot
from .session import Session, SessionSettings
from .session_store import SessionStore, ToolResultLimits
from .session_store import PersistedToolResult, build_persisted_output_message
from .checkpoint import CheckpointStore
from .tool_call_recovery import (
    UnclosedToolCall,
    build_recovery_tool_output,
    find_unclosed_tool_calls,
)


VALID_PERMISSION_MODES: set[str] = {
    "default",
    "accept_edits",
    "plan",
    "dont_ask",
    "auto_approve",
}
VALID_PLAN_STATUSES = {"pending", "in_progress", "completed", "blocked"}
VALID_PLAN_STEP_STATUSES = {"pending", "in_progress", "completed"}


@dataclass(frozen=True)
class RestoredSession:
    """一次 Transcript 重放后可直接注入 AgentEngine 的核心对象。"""

    # 已恢复的活动模型上下文和压缩状态。
    context_manager: ContextManager

    # 已重新打开且可以继续追加 Transcript 的 Session。
    session: Session

    # 已恢复 Plan、但不恢复单次 Run 缓存的工具上下文。
    tool_context: ToolExecutionContext

    # 最近一个仍可继续的 Checkpoint 运行状态；没有时为 None。
    pending_run_state: RunStateSnapshot | None = None


class SessionRestorer:
    """把 Session Transcript 确定性重放为新的进程内对象。"""

    def __init__(
        self,
        workdir: Path,
        tool_result_limits: ToolResultLimits | None = None,
    ) -> None:
        # 恢复必须继续使用原 Session 的工作区和工具结果目录。
        self.workdir = workdir.resolve()
        self.tool_result_limits = tool_result_limits

    def restore(
        self,
        session_id: str,
        checkpoint_id: str | None = None,
    ) -> RestoredSession:
        """恢复一个已存在 Session，并修复中断造成的未闭合工具协议。"""
        # 1. 严格打开当前版本 Transcript，任何损坏都不能静默跳过。
        store = SessionStore.open_existing(
            self.workdir,
            session_id,
            self.tool_result_limits,
        )
        with store.write_guard():
            return self._restore_owned(store, checkpoint_id)

    def _restore_owned(self, store, checkpoint_id):
        session_id = store.session_id
        records = store.read_transcript()

        # 2. 依照领域事件顺序重建 Context、Session 设置和 Plan 当前值。
        # 2. 优先采用最近有效 Checkpoint；不存在或损坏时回退全量重放。
        checkpoint = None
        checkpoint_store = CheckpointStore(store.session_dir)
        commits = _committed_checkpoints(records, checkpoint_id)
        for candidate_id, commit in commits:
            try:
                candidate = checkpoint_store.read(candidate_id)
                if candidate.session_id != session_id:
                    raise ValueError("Checkpoint 所属 Session 不一致")
                if candidate.transcript_sequence >= records[-1]["sequence"]:
                    raise ValueError("Checkpoint 覆盖序号超出 Transcript")
                if (
                    commit.get("transcript_sequence") != candidate.transcript_sequence
                    or commit.get("checksum") != candidate.checksum
                ):
                    raise ValueError("Checkpoint 与提交事实不一致")
                checkpoint = candidate
                break
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue

        context_manager = (
            ContextManager.from_checkpoint(checkpoint.context)
            if checkpoint is not None
            else ContextManager()
        )
        permission_mode: PermissionMode = "default"
        current_plan: Plan | None = (
            _plan_from_payload(checkpoint.plan) if checkpoint is not None else None
        )
        for record in records:
            if checkpoint is not None and record["sequence"] <= checkpoint.transcript_sequence:
                if record["type"] == "session_settings":
                    permission_mode = _permission_mode(record["payload"])
                continue
            record_type = record["type"]
            payload = record["payload"]
            if record_type == "session_settings":
                permission_mode = _permission_mode(payload)
            elif record_type == "context_user_query_appended":
                context_manager.restore_user_query(
                    _required_int(payload, "query_sequence"),
                    _required_string(payload, "content"),
                )
            elif record_type == "context_messages_appended":
                messages = payload.get("messages")
                completes_round = payload.get("completes_round")
                if not isinstance(messages, list) or not isinstance(
                    completes_round, bool
                ):
                    raise ValueError("Context 消息追加记录结构无效")
                context_manager.append_messages(
                    messages,
                    completes_round=completes_round,
                )
            elif record_type == "context_compaction_applied":
                context_manager.restore_compaction(payload)
            elif record_type == "plan_state_changed":
                current_plan = _plan_from_payload(payload.get("plan"))

        # 3. ContextRecord 不属于模型 Context；始终由完整 Transcript 重放其投影。
        context_records = ContextRecordStore()
        last_extracted_transcript_sequence = 0
        for record in records:
            if record["type"] == "context_record_extraction_committed":
                payload = record["payload"]
                through = _required_int(payload, "through_sequence")
                if not last_extracted_transcript_sequence < through < record["sequence"]:
                    raise ValueError("ContextRecord 提取进度不连续或超出事实范围")
                mutations = payload.get("mutations")
                if not isinstance(mutations, list):
                    raise ValueError("ContextRecord 提取提交缺少变更数组")
                for mutation in mutations:
                    context_records.apply_mutation(ContextRecordStore.mutation_from_payload(mutation))
                last_extracted_transcript_sequence = through
                continue
            if record["type"] not in {
                "context_record_changed",
                "context_item_changed",  # 兼容 ContextRecord 重命名前的历史事实。
            }:
                continue
            context_records.apply_mutation(
                ContextRecordStore.mutation_from_payload(record["payload"])
            )

        # 4. 仅当 Checkpoint 后没有新的状态事实时，才允许继续其运行预算。
        pending_run_state = _pending_run_state(checkpoint, records)

        # 5. 崩溃可能留下两类工具协议缺口：未 finished 的调用只能保守告知未知状态；
        #    已 finished 但 Context 未提交结果的调用，则从 durable finished fact 重建原结果。
        interrupted_outputs, interrupted_calls = _build_interrupted_tool_outputs(
            records,
            context_manager.active_messages,
        )
        finished_outputs, finished_calls = _build_finished_tool_outputs(
            records,
            context_manager.active_messages,
            store,
        )
        repair_outputs = [*interrupted_outputs, *finished_outputs]
        if repair_outputs:
            context_manager.append_messages(repair_outputs, completes_round=True)
            store.append_transcript(
                "context_messages_appended",
                {
                    "messages": repair_outputs,
                    "completes_round": True,
                    "source": "session_recovery",
                },
            )
            store.append_transcript(
                "session_recovery_repaired",
                {
                    "repaired_calls": [
                        {
                            "call_id": item.call_id,
                            "tool_name": item.tool_name,
                            "recovery_action": item.recovery_action,
                        }
                        for item in interrupted_calls
                    ]
                    + [
                        {
                            "call_id": call_id,
                            "tool_name": tool_name,
                            "recovery_action": "restore_finished_output",
                        }
                        for call_id, tool_name in finished_calls
                    ],
                },
            )

        # 6. PlanStore 只恢复当前快照；读取缓存和重复调用缓存从空状态开始。
        plan_store = InMemoryPlanStore()
        if current_plan is not None:
            plan_store.set_plan(current_plan)
        session = Session.restored(
            store,
            SessionSettings(permission_mode),
            context_records,
        )
        session.last_extracted_transcript_sequence = last_extracted_transcript_sequence
        latest_query = next(
            (query.content for query in reversed(context_manager.user_queries)),
            None,
        )
        if latest_query is not None:
            session.restore_latest_user_query(latest_query)
        session.record_runtime_event(
            "session_recovered",
            {
                "context_message_count": len(context_manager.active_messages),
                "plan_revision": (
                    current_plan.revision if current_plan is not None else None
                ),
                "repaired_tool_call_count": len(repair_outputs),
                "has_pending_run": pending_run_state is not None,
            },
        )
        return RestoredSession(
            context_manager,
            session,
            ToolExecutionContext(plan_store, self.workdir),
            pending_run_state,
        )


def _permission_mode(payload: dict[str, Any]) -> PermissionMode:
    """解析 Session 固定权限模式。"""
    mode = payload.get("permission_mode")
    if mode not in VALID_PERMISSION_MODES:
        raise ValueError(f"Session Transcript 包含无效权限模式：{mode}")
    return mode  # type: ignore[return-value]


def _committed_checkpoints(
    records: list[dict[str, Any]],
    requested_id: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    """从 Transcript 提交事实中按新到旧选择 Checkpoint。"""
    committed: list[tuple[str, dict[str, Any]]] = []
    for record in records:
        if record["type"] != "checkpoint_committed":
            continue
        payload = record["payload"]
        checkpoint_id = payload.get("checkpoint_id")
        if isinstance(checkpoint_id, str):
            committed.append((checkpoint_id, payload))
    if requested_id is not None:
        return [
            (checkpoint_id, payload)
            for checkpoint_id, payload in committed
            if checkpoint_id == requested_id
        ]
    return list(reversed(committed))


def _pending_run_state(
    checkpoint: Any | None,
    records: list[dict[str, Any]],
) -> RunStateSnapshot | None:
    """判断恢复出的 Checkpoint 是否仍能安全地继续运行。"""
    if checkpoint is None or checkpoint.run_status not in {"active", "continuable"}:
        return None
    if not isinstance(checkpoint.run_state, dict):
        return None

    # 1. 快照之后出现新的 Context/模型/工具事实时，运行计数已过期，不能猜测续跑。
    stateful_events = {
        "context_user_query_appended",
        "context_messages_appended",
        "context_compaction_applied",
        "model_response_completed",
        "agent_run_finished",
    }
    if any(
        record["sequence"] > checkpoint.transcript_sequence
        and record["type"] in stateful_events
        for record in records
    ):
        return None

    # 2. 通过严格解析后才把预算和控制状态交给下一次 run(continue)。
    return RunStateSnapshot.from_dict(checkpoint.run_state)


def _plan_from_payload(payload: Any) -> Plan | None:
    """把完整 Plan 状态反序列化为领域对象。"""
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("Plan 状态记录必须是对象或 null")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("Plan 状态记录缺少步骤列表")
    try:
        steps = []
        for step in raw_steps:
            if not isinstance(step, dict):
                raise ValueError("Plan 步骤必须全部是对象")
            step_status = _required_string(step, "status")
            if step_status not in VALID_PLAN_STEP_STATUSES:
                raise ValueError(f"Plan 步骤状态无效：{step_status}")
            steps.append(
                PlanStep(
                    content=_required_string(step, "content"),
                    process_message=_required_string(step, "process_message"),
                    status=step_status,  # type: ignore[arg-type]
                    step_id=_required_string(step, "step_id"),
                )
            )
        if len(steps) != len(raw_steps):
            raise ValueError("Plan 步骤必须全部是对象")
        plan_status = _required_string(payload, "status")
        if plan_status not in VALID_PLAN_STATUSES:
            raise ValueError(f"Plan 状态无效：{plan_status}")
        return Plan(
            objective=_required_string(payload, "objective"),
            status=plan_status,  # type: ignore[arg-type]
            steps=steps,
            revision=_required_int(payload, "revision"),
            last_replan_reason=_optional_string(payload, "last_replan_reason"),
            blocked_reason=_optional_string(payload, "blocked_reason"),
        )
    except (KeyError, TypeError) as error:
        raise ValueError("Plan 状态记录结构无效") from error


def _build_interrupted_tool_outputs(
    records: list[dict[str, Any]],
    messages: list[Any],
) -> tuple[list[dict[str, str]], tuple[UnclosedToolCall, ...]]:
    """根据执行账本为未闭合 Function Call 构造保守配对结果。"""
    calls: set[str] = set()
    completed: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("type") == "function_call" and isinstance(
            message.get("call_id"), str
        ):
            calls.add(message["call_id"])
        elif message.get("type") == "function_call_output" and isinstance(
            message.get("call_id"), str
        ):
            completed.add(message["call_id"])
    unresolved = find_unclosed_tool_calls(records, calls - completed)
    return [build_recovery_tool_output(item) for item in unresolved], unresolved


def _build_finished_tool_outputs(
    records: list[dict[str, Any]],
    messages: list[Any],
    store: SessionStore,
) -> tuple[list[dict[str, str]], tuple[tuple[str, str | None], ...]]:
    """从 durable finished fact 补回尚未提交到 Context 的工具结果。

    finished 是工具真实执行已结束的权威事实；恢复只能重放其模型可见结果，
    绝不能重新 dispatch、重新评估权限或推测新的工具输出。
    """
    calls, completed = _tool_call_ids(messages)
    repaired: list[dict[str, str]] = []
    repaired_calls: list[tuple[str, str | None]] = []
    for record in records:
        if record.get("type") != "tool_execution_finished":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        call_id = payload.get("call_id")
        if (
            not isinstance(call_id, str)
            or call_id not in calls
            or call_id in completed
        ):
            continue

        # 1. 每个 call_id 在 Transcript 中最多补一次，即使损坏日志存在重复 finished。
        if any(item["call_id"] == call_id for item in repaired):
            continue
        tool_name = payload.get("tool_name")
        repaired.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": _finished_model_output(payload, records, store),
            }
        )
        repaired_calls.append((call_id, tool_name if isinstance(tool_name, str) else None))
    return repaired, tuple(repaired_calls)


def _tool_call_ids(messages: list[Any]) -> tuple[set[str], set[str]]:
    """提取活动 Context 中的 function call 及其已提交结果身份。"""
    calls: set[str] = set()
    completed: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        call_id = message.get("call_id")
        if not isinstance(call_id, str):
            continue
        if message.get("type") == "function_call":
            calls.add(call_id)
        elif message.get("type") == "function_call_output":
            completed.add(call_id)
    return calls, completed


def _finished_model_output(
    finished: dict[str, Any],
    records: list[dict[str, Any]],
    store: SessionStore,
) -> str:
    """恢复正常工具路径本应写入 Context 的模型可见输出。"""
    output = finished.get("output")
    if isinstance(output, str):
        return output

    persisted_output = finished.get("persisted_output")
    if isinstance(persisted_output, str):
        return _read_persisted_tool_output(
            persisted_output,
            _persisted_artifact_id(records, finished.get("call_id")),
            store,
        )

    tool_name = finished.get("tool_name")
    label = tool_name if isinstance(tool_name, str) else "未知工具"
    return f"错误：工具 {label} 已完成，但其结果正文无法从 durable Transcript 恢复。"


def _persisted_artifact_id(
    records: list[dict[str, Any]],
    call_id: Any,
) -> str:
    """从同一 call_id 的持久化工件映射读取 artifact_id，仅用于重建展示消息。"""
    if not isinstance(call_id, str):
        return ""
    for record in reversed(records):
        if record.get("type") != "tool_result_persisted":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("call_id") != call_id:
            continue
        artifact_id = payload.get("artifact_id")
        return artifact_id if isinstance(artifact_id, str) else ""
    return ""


def _read_persisted_tool_output(
    relative_path: str,
    artifact_id: str,
    store: SessionStore,
) -> str:
    """安全读取外置工件，并按正常路径重建 persisted-output 引用。"""
    path = (store.workdir / relative_path).resolve()
    try:
        if not path.is_relative_to(store.workdir) or not path.is_file():
            raise OSError("外置工具结果工件不存在")
        output = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        return f"错误：工具已完成，但外置结果工件不可读取：{error}"
    return build_persisted_output_message(
        PersistedToolResult(
            relative_path=relative_path,
            original_characters=len(output),
            preview=output[: store.tool_result_limits.preview_characters],
            artifact_id=artifact_id,
        )
    )


def _required_string(payload: dict[str, Any], key: str) -> str:
    """读取必填非空字符串。"""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"恢复记录字段 {key} 必须是非空字符串")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    """读取可选字符串。"""
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"恢复记录字段 {key} 必须是字符串或 null")
    return value


def _required_int(payload: dict[str, Any], key: str) -> int:
    """读取必填整数，并拒绝 bool 冒充整数。"""
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"恢复记录字段 {key} 必须是整数")
    return value
