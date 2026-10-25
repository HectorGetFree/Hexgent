"""Phase4 Context / Memory 真实运行时边界的确定性场景。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Callable

from hexgent.context import ContextLimits, ContextManager
from hexgent.context.context_compaction import compact_context_if_needed
from hexgent.memory import ContextRecordInput, ContextRetrievalLimits
from hexgent.plan import Plan, PlanStep
from hexgent.session import Session, SessionRestorer
from hexgent.tool.builtin_tools import read_file, write_file
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.plan import InMemoryPlanStore
from tests.support.fact_extraction_fixture import extraction_event


@dataclass(frozen=True)
class ContextMemoryObservation:
    """一个场景从真实 Runtime API 收集的最小可断言事实。"""

    facts: dict[str, Any]


@dataclass(frozen=True)
class ContextMemoryScenario:
    """一条不可依赖 LLM 判断的 Context / Memory 合约。"""

    name: str
    invariant: str
    execute: Callable[[], ContextMemoryObservation]
    verify: Callable[[ContextMemoryObservation], None]


class _FakeResponses:
    """为正式压缩器提供固定的 completed 响应。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **arguments: Any):
        extraction = extraction_event(self, arguments)
        if extraction is not None:
            return extraction
        self.calls.append(arguments)
        response = SimpleNamespace(
            output_text="<summary>已完成此前文件读取与结论。</summary>",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )
        return iter([SimpleNamespace(type="response.completed", response=response)])


def _drain(generator: Any) -> Any:
    """消费 Generator 并返回其正式 CompactionOutcome。"""

    while True:
        try:
            next(generator)
        except StopIteration as stopped:
            return stopped.value


def compaction_preserves_complete_tool_round() -> ContextMemoryScenario:
    """压缩切点不得把 function_call 与 output 拆开。"""

    def execute() -> ContextMemoryObservation:
        context = ContextManager()
        call = SimpleNamespace(
            type="function_call", call_id="read-1", name="read_file",
            arguments='{"path":"README.md"}',
        )
        output = {
            "type": "function_call_output", "call_id": "read-1",
            "output": "README 正文" * 80,
        }
        context.append_user_query("读取 README 后总结")
        context.append_messages([call, output], completes_round=True)
        context.append_user_query("给出最终摘要")
        responses = _FakeResponses()
        client = SimpleNamespace(responses=responses)
        outcome = _drain(compact_context_if_needed(
            context, client, "fake", "系统提示", [],
            ContextLimits(
                context_window_tokens=500, reserved_output_tokens=1,
                safety_margin_tokens=1, recent_context_tokens=100,
                proactive_threshold_tokens=100,
                min_compaction_savings_tokens=1,
                min_compaction_savings_ratio=0.01,
            ),
        ))
        summary_input = responses.calls[0]["input"]
        return ContextMemoryObservation({
            "status": outcome.status,
            "summary_has_call": call in summary_input,
            "summary_has_output": output in summary_input,
            "latest_query": context.user_queries[-1].content,
        })

    def verify(observation: ContextMemoryObservation) -> None:
        facts = observation.facts
        assert facts["status"] in {"compacted", "partial"}
        assert facts["summary_has_call"] and facts["summary_has_output"]
        assert facts["latest_query"] == "给出最终摘要"

    return ContextMemoryScenario(
        "compaction_preserves_complete_tool_round",
        "压缩不得拆开同一轮的工具调用与 function_call_output，并保留最新 Query",
        execute, verify,
    )


def restore_after_compaction_preserves_context_and_plan() -> ContextMemoryScenario:
    """已提交压缩及 Plan 必须从 durable facts 恢复为同一投影。"""

    def execute() -> ContextMemoryObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            context = ContextManager()
            session = Session.persistent(root)
            first = context.append_user_query("较早问题")
            session.record_user_query(first)
            answer = {"role": "assistant", "content": "较早回答"}
            context.append_messages([answer], completes_round=True)
            session.record_context_messages([answer], completes_round=True, source="model_response")
            latest = context.append_user_query("最新问题不可改写")
            session.record_user_query(latest)
            plan = Plan(
                "恢复后的计划", "in_progress",
                [PlanStep("检查", "正在检查", "in_progress", "step-1")],
            )
            session.record_plan_state(plan, action="created")
            boundary = context.apply_compaction(
                2, "较早问题已经处理。", before_tokens=100, after_tokens=30,
                summary_tokens=10, recent_tokens=5, status="compacted",
            )
            session.record_compaction(boundary)
            expected_messages = context.model_messages()
            expected_boundaries = list(context.round_boundaries)
            restored = SessionRestorer(root).restore(session.store.session_id)
            return ContextMemoryObservation({
                "messages_equal": restored.context_manager.model_messages() == expected_messages,
                "boundaries_equal": restored.context_manager.round_boundaries == expected_boundaries,
                "latest_query": restored.context_manager.user_queries[-1].content,
                "plan_equal": restored.tool_context.plan_store.get_plan() == plan,
            })

    def verify(observation: ContextMemoryObservation) -> None:
        facts = observation.facts
        assert facts["messages_equal"] and facts["boundaries_equal"]
        assert facts["latest_query"] == "最新问题不可改写"
        assert facts["plan_equal"]

    return ContextMemoryScenario(
        "restore_after_compaction_preserves_context_and_plan",
        "压缩后的 Context 与活动 Plan 必须由 Transcript/Checkpoint 等 durable facts 精确恢复",
        execute, verify,
    )


def superseded_context_record_is_not_recalled() -> ContextMemoryScenario:
    """只有 active 的 Session ContextRecord 可进入后续检索。"""

    def execute() -> ContextMemoryObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = Session.persistent(root)
            query = ContextManager().append_user_query("记录当前部署端口")
            session.record_user_query(query)
            original = session.upsert_context_records([
                ContextRecordInput("服务部署端口是 8080。", "fact", (1,)),
            ])[0].record
            session.upsert_context_records([
                ContextRecordInput(
                    "服务部署端口是 8098。", "fact", (1,), (original.record_id,),
                ),
            ])
            restored = SessionRestorer(root).restore(session.store.session_id)
            records = restored.session.retrieve_context_records(
                "当前部署端口是什么", (1,), ContextRetrievalLimits(enabled=True),
            )
            return ContextMemoryObservation({
                "recalled": tuple(record.content for record in records),
                "active": tuple(record.content for record in restored.session.context_records.active_records),
                "all_statuses": tuple(
                    (record.content, record.status)
                    for record in restored.session.context_records.records
                ),
            })

    def verify(observation: ContextMemoryObservation) -> None:
        facts = observation.facts
        assert facts["active"] == ("服务部署端口是 8098。",)
        assert facts["recalled"] == ("服务部署端口是 8098。",)
        assert ("服务部署端口是 8080。", "superseded") in facts["all_statuses"]

    return ContextMemoryScenario(
        "superseded_context_record_is_not_recalled",
        "被显式 supersede 的 Session ContextRecord 不得被新 Query 召回；恢复只采用 durable mutation",
        execute, verify,
    )


def file_read_reuse_detects_write_invalidation() -> ContextMemoryScenario:
    """读取复用只能作用于未变内容；写入后必须重新暴露新正文。"""

    def execute() -> ContextMemoryObservation:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "notes.txt"
            target.write_text("旧内容", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            first = read_file("notes.txt", context)
            reused = read_file("notes.txt", context)
            written = write_file("notes.txt", "新内容", context)
            refreshed = read_file("notes.txt", context)
            return ContextMemoryObservation({
                "first": first.output,
                "reused": reused.output,
                "write_ok": written.ok,
                "refreshed": refreshed.output,
            })

    def verify(observation: ContextMemoryObservation) -> None:
        facts = observation.facts
        assert facts["first"] == "旧内容"
        assert "file-unchanged" in facts["reused"]
        assert facts["write_ok"]
        assert facts["refreshed"] == "新内容"
        assert "file-unchanged" not in facts["refreshed"]

    return ContextMemoryScenario(
        "file_read_reuse_detects_write_invalidation",
        "相同内容可避免重复注入；文件副作用后内容哈希必须阻止旧读结果复用",
        execute, verify,
    )


def workspace_memory_is_not_session_isolated() -> ContextMemoryScenario:
    """显式记录当前 Memory 的真实作用域，避免把工作区共享误报为隔离。"""

    def execute() -> ContextMemoryObservation:
        return ContextMemoryObservation({
            "scope": "workspace",
            "reason": "MemoryStore 位于 <workdir>/.hexgent/memory，跨 Session 设计为可召回",
        })

    def verify(_observation: ContextMemoryObservation) -> None:
        # 这是实现边界记录，不应将工作区级 Memory 误标为 Session 隔离 PASS。
        return None

    return ContextMemoryScenario(
        "workspace_memory_session_isolation",
        "长期 Memory 的跨 Session 可见性需要独立产品作用域；当前实现为工作区级而非 Session 隔离",
        execute, verify,
    )


def v1_scenarios() -> tuple[ContextMemoryScenario, ...]:
    """返回 Phase4 V1 覆盖的正确性与复用边界。"""

    return (
        compaction_preserves_complete_tool_round(),
        restore_after_compaction_preserves_context_and_plan(),
        superseded_context_record_is_not_recalled(),
        file_read_reuse_detects_write_invalidation(),
        workspace_memory_is_not_session_isolated(),
    )
