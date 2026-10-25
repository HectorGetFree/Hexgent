"""Agent Loop 状态机的公开流式入口。"""

from collections.abc import Generator
from dataclasses import replace
from typing import Any

from ..client import get_client
from ..config import HexgentConfig
from ..run_control import RunControl
from ..session import CheckpointReason, Session
from ..tool.tool_execution import UserConfirmation, unavailable_user_confirmation
from hexgent.events import (
    AgentEvent,
    CheckpointCommittedEvent,
    LoopTransitionEvent,
    RunFinishedEvent,
)
from .loop_state import LoopState
from .transitions import Continue, Stop
from .agent_turn import _control_stop, run_turn
from .extract_context_records import extract_context_records
from ..observation import current_observation


CHECKPOINT_REASON_BY_TRANSITION: dict[str, CheckpointReason] = {
    "tool_results_ready": "tool_batch_completed",
    "plan_continuation": "plan_continuation_ready",
    "context_compacted": "context_compaction_completed",
    "reactive_context_compacted": "reactive_compaction_completed",
    "budget_finalization": "finalization_ready",
    "steering_applied": "steering_applied",
}


def agent_loop(
    state: LoopState,
    *,
    config: HexgentConfig,
    client: Any | None = None,
    session: Session | None = None,
    user_confirmation: UserConfirmation = unavailable_user_confirmation,
    run_control: RunControl | None = None,
) -> Generator[AgentEvent, None, Stop]:
    """持续驱动单轮状态迁移，并在唯一出口发送最终事件。"""
    # 1. 在唯一入口补齐稳定依赖，并初始化本次 Run 的工具缓存与 Session。
    active_client = client if client is not None else get_client()
    active_control = run_control or RunControl()
    state.tool_context.clear_tool_call_cache()
    if session is not None:
        session.start_run(
            limits_enabled=config.loop.enforce_run_limits,
            plan=state.tool_context.plan_store.get_plan(),
            loop_state=state,
        )
        observation = current_observation.get()
        if observation is not None:
            observation.emit("run_binding", run_id=session._active_run_id,
                             session_id=session.store.session_id if session.store else None)

    while True:
        # 2. 每轮只产生 Continue 或 Stop，yield 事件不参与控制流判断。
        turn_output = yield from run_turn(
            state,
            config=config,
            client=active_client,
            session=session,
            user_confirmation=user_confirmation,
            run_control=active_control,
        )

        # 3. 完整 Turn 已落盘；提取稳定事实后，再写入下一状态 Checkpoint。
        next_state = (
            turn_output.next_state
            if isinstance(turn_output, Continue)
            else turn_output.state
        )
        if (
            session is not None
            and not active_control.interrupt_controller.interrupted
            and next_state.model_rounds > state.model_rounds
        ):
            session.invalidate_run_state("context_record_extraction")
            extraction_input_tokens, extraction_output_tokens = (
                extract_context_records(
                    session,
                    active_client,
                    config.context_extraction,
                    active_control.interrupt_controller,
                    force=False,
                )
            )
            next_state = replace(
                next_state,
                input_tokens=next_state.input_tokens + extraction_input_tokens,
                output_tokens=next_state.output_tokens + extraction_output_tokens,
            )
            if active_control.interrupt_controller.interrupted:
                turn_output = _control_stop(
                    next_state,
                    active_control,
                    session,
                    "context_record_extraction",
                )
            elif isinstance(turn_output, Continue):
                turn_output = Continue(next_state, turn_output.reason)
            else:
                turn_output = Stop(
                    replace(
                        turn_output.terminal,
                        elapsed_seconds=next_state.elapsed_seconds,
                    ),
                    next_state,
                )
        if session is not None:
            session.record_run_state(next_state, run_status=("active" if isinstance(turn_output, Continue)
                else "continuable" if turn_output.terminal.status in {"interrupted", "incomplete"} else "terminal"))
        if isinstance(turn_output, Continue):
            yield LoopTransitionEvent(turn_output.reason)
            state = turn_output.next_state
            if session is not None:
                try:
                    checkpoint = session.create_checkpoint(
                        context_manager=state.context_manager,
                        plan=state.tool_context.plan_store.get_plan(),
                        reason=CHECKPOINT_REASON_BY_TRANSITION[turn_output.reason],
                        loop_state=state,
                        run_status="active",
                    )
                    if checkpoint is not None:
                        yield CheckpointCommittedEvent(
                            checkpoint.checkpoint_id,
                            checkpoint.transcript_sequence,
                            checkpoint.reason,
                        )
                except Exception as error:
                    session.record_runtime_event(
                        "checkpoint_failed",
                        {"message": f"写入 Checkpoint 失败：{error}"},
                    )
                    if turn_output.reason in {"context_compacted", "reactive_context_compacted"}:
                        # 已提交 Compaction 不回滚；停止并由正式 Restore 重建。
                        raise RuntimeError("COMPACTION_COMMITTED_RESTORE_REQUIRED：Checkpoint 失败") from error
            continue

        # 4. Stop 是 Agent Loop 的唯一正常出口。
        if session is not None:
            session.finish_run(
                status=turn_output.terminal.status,
                stop_reason=turn_output.terminal.stop_reason,
                elapsed_seconds=turn_output.terminal.elapsed_seconds,
                output_text=turn_output.terminal.output_text,
                plan=state.tool_context.plan_store.get_plan(),
                context_manager=turn_output.state.context_manager,
                loop_state=turn_output.state,
            )
        yield RunFinishedEvent(turn_output.terminal)
        return turn_output
