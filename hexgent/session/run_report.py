"""从一次 Agent Run 的 Transcript 事实构造结构化报告。"""

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any


RUN_REPORT_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class RunReport:
    """一次用户 Query 驱动的 Agent Run 汇总工件。"""

    # Report 数据结构版本，供后续兼容迁移使用。
    schema_version: int

    # 当前 Report 对应的 Session 内稳定运行编号。
    run_id: str

    # 当前 Run 所属的 Session 标识。
    session_id: str

    # 当前 Run 最终进入的终止状态。
    status: str

    # 当前 Run 开始和结束时的北京时间。
    started_at: str
    finished_at: str

    # 当前 Run 从开始到结束的总耗时秒数。
    elapsed_seconds: float

    # 触发当前 Run 的用户原始 Query。
    user_query: str

    # 当前 Run 的明确停止原因；正常完成时为空。
    stop_reason: str | None

    # 模型轮数与输入、输出 Token 的聚合统计。
    usage: dict[str, int]

    # Run 开始和结束时的 Plan 快照。
    plan: dict[str, Any]

    # 工具调用总数以及按名称、状态、失败码聚合的计数。
    tools: dict[str, Any]

    # 上下文压缩和大结果外置的聚合事实。
    context: dict[str, Any]

    # 模型调用失败和工具执行错误的结构化列表。
    errors: list[dict[str, Any]]

    # 当前 Run 引用的去重后工具结果工件。
    artifacts: list[dict[str, Any]]

    # 当前 Run 最终返回给用户的完整文本。
    final_output: str

    def to_dict(self) -> dict[str, Any]:
        """转换为能够直接写入 JSON 的基础字典。"""
        return asdict(self)


def build_run_report(
    records: list[dict[str, Any]],
    *,
    run_id: str,
    session_id: str,
) -> RunReport:
    """从一个完整 Run 的 Transcript 记录构造确定性报告。"""
    # 1. 定位 Run 边界；调用方只传入当前 Run 的连续记录。
    started = next(
        record for record in records if record["type"] == "agent_run_started"
    )
    finished = next(
        record
        for record in reversed(records)
        if record["type"] == "agent_run_finished"
    )
    started_payload = started["payload"]
    finished_payload = finished["payload"]

    # 2. 聚合完整模型响应报告的真实用量，不估算失败响应的 Token。
    model_responses = [
        record
        for record in records
        if record["type"] == "model_response_completed"
    ]
    input_tokens = 0
    output_tokens = 0
    for record in model_responses:
        usage = record["payload"].get("usage") or {}
        if isinstance(usage, dict):
            input_tokens += usage.get("input_tokens", 0) or 0
            output_tokens += usage.get("output_tokens", 0) or 0

    # 3. 按工具名称和最终状态聚合调用，不复制可能很大的工具正文。
    tool_records = [
        record
        for record in records
        if record["type"] == "tool_execution_finished"
    ]
    tools_by_name = Counter(
        record["payload"].get("tool_name", "unknown") for record in tool_records
    )
    tools_by_status = Counter(
        record["payload"].get("status", "unknown") for record in tool_records
    )
    tools_by_failure_code = Counter(
        failure["code"]
        for record in tool_records
        if isinstance((failure := record["payload"].get("failure")), dict)
        and isinstance(failure.get("code"), str)
    )

    # 4. 保存每次压缩的前后预算，并对内容寻址工件按 artifact_id 去重。
    compactions = [
        {
            "before_tokens": record["payload"].get("before_tokens"),
            "after_tokens": record["payload"].get("after_tokens"),
            "summary_tokens": record["payload"].get("summary_tokens"),
            "recent_tokens": record["payload"].get("recent_tokens"),
            "status": record["payload"].get("status"),
        }
        for record in records
        if record["type"] == "context_compaction_applied"
    ]
    persisted_records = [
        record["payload"]
        for record in records
        if record["type"] == "tool_result_persisted"
    ]
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    for persisted in persisted_records:
        artifact_id = persisted["artifact_id"]
        artifact = artifacts_by_id.setdefault(
            artifact_id,
            {
                "artifact_id": artifact_id,
                "tool_name": persisted.get("tool_name"),
                "relative_path": persisted.get("relative_path"),
                "original_characters": persisted.get("original_characters", 0),
                "call_ids": [],
            },
        )
        artifact["call_ids"].append(persisted.get("call_id"))

    # 5. 错误只保存稳定诊断字段，避免把完整工具结果再次写入 Report。
    errors = [
        {
            "type": "model_response_failed",
            "message": record["payload"].get("message"),
            "reason": record["payload"].get("reason"),
            "recoverable": record["payload"].get("recoverable", False),
        }
        for record in records
        if record["type"] == "model_response_failed"
    ]
    errors.extend(
        {
            "type": "tool_execution_failed",
            "tool_name": record["payload"].get("tool_name"),
            "call_id": record["payload"].get("call_id"),
            "status": record["payload"].get("status"),
            "failure": record["payload"].get("failure"),
        }
        for record in tool_records
        if record["payload"].get("status") in {"invalid", "denied", "error"}
    )

    # 6. Plan 可能在正常退出前从 PlanStore 清除，此时使用最后一次持久化快照。
    plan_records = [
        record["payload"].get("plan")
        for record in records
        if record["type"] == "plan_state_changed"
        and isinstance(record["payload"].get("plan"), dict)
    ]
    cancelled_plan_records = [
        record["payload"].get("plan")
        for record in records
        if record["type"] == "plan_cancelled"
        and isinstance(record["payload"].get("plan"), dict)
    ]
    final_plan = finished_payload.get("plan")
    if final_plan is None and cancelled_plan_records:
        final_plan = cancelled_plan_records[-1]
    if final_plan is None and plan_records:
        final_plan = plan_records[-1]

    return RunReport(
        schema_version=RUN_REPORT_SCHEMA_VERSION,
        run_id=run_id,
        session_id=session_id,
        status=finished_payload["status"],
        started_at=started["created_at"],
        finished_at=finished["created_at"],
        elapsed_seconds=finished_payload["elapsed_seconds"],
        user_query=started_payload["user_query"],
        stop_reason=finished_payload.get("stop_reason"),
        usage={
            "model_rounds": len(model_responses),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        plan={
            "initial": started_payload.get("plan"),
            "final": final_plan,
        },
        tools={
            "total": len(tool_records),
            "by_name": dict(sorted(tools_by_name.items())),
            "by_status": dict(sorted(tools_by_status.items())),
            "by_failure_code": dict(sorted(tools_by_failure_code.items())),
        },
        context={
            "compaction_count": len(compactions),
            "compactions": compactions,
            "persisted_output_count": len(persisted_records),
            "unique_artifact_count": len(artifacts_by_id),
        },
        errors=errors,
        artifacts=list(artifacts_by_id.values()),
        final_output=finished_payload.get("output_text", ""),
    )
