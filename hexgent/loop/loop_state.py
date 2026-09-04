"""Agent Loop 跨轮次继承的运行状态。"""

from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Literal

from hexgent.context.context_manager import ContextManager

from ..memory import RetrievedContextRecord
from ..tool.tool_context import ToolExecutionContext
from .limits import LoopStopReason

LoopMode = Literal[
    "working",  # 正常调用模型、执行工具并推进 Plan。
    "finalizing",  # 已停止正常循环，只进行一次禁用工具的收尾响应。
]
ContinueReason = Literal[
    "tool_results_ready",  # 工具批次结果已回填，继续下一轮模型调用。
    "plan_continuation",  # Plan 仍在执行中，提醒模型继续推进或更新状态。
    "context_compacted",  # 活动上下文已压缩，使用新上下文重试当前轮次。
    "reactive_context_compacted",  # API 拒绝过长请求后，应急压缩并重试一次。
    "budget_finalization",  # 命中循环预算，进入禁用工具的收尾轮次。
    "steering_applied",  # FIFO 中的新 Query 已在完整 Turn 边界注入 Context。
]


@dataclass(frozen=True)
class RunStateSnapshot:
    """Checkpoint 中保存的可续跑 LoopState 字段。"""

    # 已完成模型轮次、工具调用和累计 Token，继续时继承原预算消耗。
    model_rounds: int
    tool_calls: int
    input_tokens: int
    output_tokens: int

    # Plan、上下文恢复和收尾控制相关的跨轮次状态。
    plan_completion_nudges: int
    reactive_compaction_attempted: bool
    mode: LoopMode
    transition: ContinueReason | None
    stop_reason: LoopStopReason | None

    # 当前 Run 固定使用的历史记录召回结果，continue 时不重新检索。
    relevant_context: tuple[RetrievedContextRecord, ...]

    # 单调时钟不可持久化，改为保存已经流逝的秒数。
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定的 Checkpoint JSON 对象。"""
        return {
            "model_rounds": self.model_rounds,
            "tool_calls": self.tool_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "plan_completion_nudges": self.plan_completion_nudges,
            "reactive_compaction_attempted": self.reactive_compaction_attempted,
            "mode": self.mode,
            "transition": self.transition,
            "stop_reason": self.stop_reason,
            "relevant_context": [
                record.to_dict() for record in self.relevant_context
            ],
            "elapsed_seconds": self.elapsed_seconds,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunStateSnapshot":
        """严格解析持久化的运行状态快照。"""
        try:
            snapshot = cls(
                model_rounds=_snapshot_int(payload, "model_rounds"),
                tool_calls=_snapshot_int(payload, "tool_calls"),
                input_tokens=_snapshot_int(payload, "input_tokens"),
                output_tokens=_snapshot_int(payload, "output_tokens"),
                plan_completion_nudges=_snapshot_int(
                    payload,
                    "plan_completion_nudges",
                ),
                reactive_compaction_attempted=payload["reactive_compaction_attempted"],
                mode=payload["mode"],
                transition=payload["transition"],
                stop_reason=payload["stop_reason"],
                relevant_context=tuple(
                    RetrievedContextRecord.from_dict(record)
                    for record in payload.get("relevant_context", [])
                ),
                elapsed_seconds=float(payload["elapsed_seconds"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Checkpoint 运行状态结构无效") from error
        if (
            not isinstance(snapshot.reactive_compaction_attempted, bool)
            or snapshot.mode not in {"working", "finalizing"}
            or snapshot.transition not in {
                None,
                "tool_results_ready",
                "plan_continuation",
                "context_compacted",
                "reactive_context_compacted",
                "budget_finalization",
                "steering_applied",
            }
            or snapshot.stop_reason not in {
                None,
                "max_model_rounds",
                "max_tool_calls",
                "max_total_tokens",
                "max_elapsed_seconds",
            }
            or snapshot.elapsed_seconds < 0
        ):
            raise ValueError("Checkpoint 运行状态字段无效")
        return snapshot


@dataclass(frozen=True)
class LoopState:
    """只保存下一轮模型调用必须继承的数据。"""

    # 跨轮次共享的活动模型上下文与压缩状态。
    context_manager: ContextManager

    # 跨轮次共享的工具上下文，包括工作区和 PlanStore。
    tool_context: ToolExecutionContext

    # 当前 Query 召回且在整个 Run 中保持稳定的历史上下文记录。
    relevant_context: tuple[RetrievedContextRecord, ...] = ()

    # 已收到完整响应的正常模型调用次数。
    model_rounds: int = 0

    # 已计入预算的真实执行和去重工具调用次数。
    tool_calls: int = 0

    # 完整模型响应累计报告的输入 Token。
    input_tokens: int = 0

    # 完整模型响应累计报告的输出 Token。
    output_tokens: int = 0

    # Plan 未完成时已经发送的继续执行提醒次数。
    plan_completion_nudges: int = 0

    # 本次 Agent Run 是否已经因 prompt_too_long 执行过一次应急压缩。
    reactive_compaction_attempted: bool = False

    # working 表示正常工具循环，finalizing 表示无工具收尾轮。
    mode: LoopMode = "working"

    # 上一轮构造当前 State 的明确原因，第一轮为空。
    transition: ContinueReason | None = None

    # 本次 Agent Run 的开始时间，用于计算总耗时。
    started_at: float = field(default_factory=monotonic)

    # 进入 finalizing 时保存的预算停止原因。
    stop_reason: LoopStopReason | None = None

    def __post_init__(self) -> None:
        """校验工作模式和停止原因之间的状态不变量。"""
        if self.mode == "working" and self.stop_reason is not None:
            raise ValueError("working State 不能包含 stop_reason")
        if self.mode == "finalizing" and self.stop_reason is None:
            raise ValueError("finalizing State 必须包含 stop_reason")
        if self.transition == "budget_finalization" and self.mode != "finalizing":
            raise ValueError("budget_finalization 必须进入 finalizing 模式")

    @property
    def messages(self) -> list[Any]:
        """返回下一轮发送给模型的活动消息引用。"""
        return self.context_manager.active_messages

    @property
    def total_tokens(self) -> int:
        """返回本次运行累计使用的 Token。"""
        return self.input_tokens + self.output_tokens

    @property
    def elapsed_seconds(self) -> float:
        """返回本次运行已经持续的秒数。"""
        return monotonic() - self.started_at

    def usage_values(self, usage: Any) -> tuple[int, int]:
        """将对象或字典形式的模型用量转换为 Token 数量。"""
        if usage is None:
            return 0, 0
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)
        else:
            input_tokens = getattr(usage, "input_tokens", 0)
            output_tokens = getattr(usage, "output_tokens", 0)
        return input_tokens or 0, output_tokens or 0

    def to_snapshot(self) -> RunStateSnapshot:
        """提取可持久化的运行状态，不序列化 Context 和工具对象。"""
        return RunStateSnapshot(
            self.model_rounds,
            self.tool_calls,
            self.input_tokens,
            self.output_tokens,
            self.plan_completion_nudges,
            self.reactive_compaction_attempted,
            self.mode,
            self.transition,
            self.stop_reason,
            self.relevant_context,
            self.elapsed_seconds,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: RunStateSnapshot,
        *,
        context_manager: ContextManager,
        tool_context: ToolExecutionContext,
    ) -> "LoopState":
        """使用新进程对象重建下一轮需要的 LoopState。"""
        return cls(
            context_manager=context_manager,
            tool_context=tool_context,
            model_rounds=snapshot.model_rounds,
            tool_calls=snapshot.tool_calls,
            input_tokens=snapshot.input_tokens,
            output_tokens=snapshot.output_tokens,
            plan_completion_nudges=snapshot.plan_completion_nudges,
            reactive_compaction_attempted=snapshot.reactive_compaction_attempted,
            mode=snapshot.mode,
            transition=snapshot.transition,
            started_at=monotonic() - snapshot.elapsed_seconds,
            stop_reason=snapshot.stop_reason,
            relevant_context=snapshot.relevant_context,
        )


def _snapshot_int(payload: dict[str, Any], key: str) -> int:
    """读取非负整数，拒绝 bool 冒充整数。"""
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{key} 必须是非负整数")
    return value
