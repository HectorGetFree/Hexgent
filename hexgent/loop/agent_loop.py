"""Agent Loop 状态机的公开流式入口。"""

from collections.abc import Generator
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
from .agent_turn import run_turn


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
        )

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
            continue

        # 3. Stop 是 Agent Loop 的唯一正常出口。
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
