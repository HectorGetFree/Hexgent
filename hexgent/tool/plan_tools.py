"""创建 Plan 和动态维护执行计划的工具。"""

from typing import Any

from hexgent.plan import Plan, PlanStatus, PlanStep, PlanStepStatus
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_result import ToolHandlerResult


def _parse_steps(steps: list[dict[str, Any]]) -> list[PlanStep]:
    """将工具参数解析为严格的 PlanStep 列表。"""
    # 1. 校验列表和每个步骤的字段结构。
    if not isinstance(steps, list) or not steps:
        raise ValueError("Plan 至少需要一个执行步骤")
    allowed_fields = {"content", "process_message", "status"}
    valid_statuses: set[PlanStepStatus] = {"pending", "in_progress", "completed"}
    parsed_steps: list[PlanStep] = []
    for index, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"第 {index} 个步骤必须是对象")
        fields = set(raw_step)
        if fields != allowed_fields:
            missing = allowed_fields - fields
            extra = fields - allowed_fields
            details = []
            if missing:
                details.append(f"缺少字段：{', '.join(sorted(missing))}")
            if extra:
                details.append(f"包含额外字段：{', '.join(sorted(extra))}")
            raise ValueError(f"第 {index} 个步骤字段无效：{'；'.join(details)}")
        content = raw_step["content"]
        process_message = raw_step["process_message"]
        status = raw_step["status"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"第 {index} 个步骤的 content 不能为空")
        if not isinstance(process_message, str) or not process_message.strip():
            raise ValueError(f"第 {index} 个步骤的 process_message 不能为空")
        if status not in valid_statuses:
            raise ValueError(f"第 {index} 个步骤的 status 无效：{status}")
        parsed_steps.append(
            PlanStep(
                content.strip(),
                process_message.strip(),
                status,
                f"step-{index}",
            )
        )

    # 2. 拒绝内容相同的重复步骤。
    contents = [step.content for step in parsed_steps]
    if len(contents) != len(set(contents)):
        raise ValueError("执行步骤中不能包含重复的 content")
    return parsed_steps


def _parse_updated_steps(
    steps: list[dict[str, Any]],
    current_plan: Plan,
) -> list[PlanStep]:
    """解析重规划快照，并保护已有步骤的身份和完成事实。"""
    # 1. 校验更新步骤的完整字段，并建立已有步骤索引。
    if not isinstance(steps, list) or not steps:
        raise ValueError("Plan 至少需要一个执行步骤")
    allowed_fields = {"step_id", "content", "process_message", "status"}
    valid_statuses: set[PlanStepStatus] = {"pending", "in_progress", "completed"}
    existing_steps = {step.step_id: step for step in current_plan.steps}
    retained_ids: set[str] = set()
    parsed_steps: list[PlanStep] = []
    new_step_number = 1

    for index, raw_step in enumerate(steps, start=1):
        if not isinstance(raw_step, dict):
            raise ValueError(f"第 {index} 个步骤必须是对象")
        fields = set(raw_step)
        if fields != allowed_fields:
            missing = allowed_fields - fields
            extra = fields - allowed_fields
            details = []
            if missing:
                details.append(f"缺少字段：{', '.join(sorted(missing))}")
            if extra:
                details.append(f"包含额外字段：{', '.join(sorted(extra))}")
            raise ValueError(f"第 {index} 个步骤字段无效：{'；'.join(details)}")

        step_id = raw_step["step_id"]
        content = raw_step["content"]
        process_message = raw_step["process_message"]
        status = raw_step["status"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"第 {index} 个步骤的 content 不能为空")
        if not isinstance(process_message, str) or not process_message.strip():
            raise ValueError(f"第 {index} 个步骤的 process_message 不能为空")
        if status not in valid_statuses:
            raise ValueError(f"第 {index} 个步骤的 status 无效：{status}")

        # 2. 已有步骤保留身份和内容；新增步骤由系统分配稳定 ID。
        if step_id is None:
            step_id = f"step-r{current_plan.revision + 1}-{new_step_number}"
            new_step_number += 1
        elif not isinstance(step_id, str) or not step_id.strip():
            raise ValueError(f"第 {index} 个步骤的 step_id 必须是字符串或 null")
        elif step_id not in existing_steps:
            raise ValueError(f"第 {index} 个步骤引用了未知 step_id：{step_id}")
        else:
            step_id = step_id.strip()
            existing_step = existing_steps[step_id]
            if content.strip() != existing_step.content:
                raise ValueError(f"已有步骤 {step_id} 的 content 不可改写")
            if existing_step.status == "completed" and status != "completed":
                raise ValueError(f"已完成步骤 {step_id} 不能回退为 {status}")

        if step_id in retained_ids:
            raise ValueError(f"执行步骤中不能包含重复的 step_id：{step_id}")
        retained_ids.add(step_id)
        parsed_steps.append(
            PlanStep(content.strip(), process_message.strip(), status, step_id)
        )

    # 3. 已完成步骤属于执行事实，后续重规划不得将其删除。
    missing_completed = [
        step.step_id
        for step in current_plan.steps
        if step.status == "completed" and step.step_id not in retained_ids
    ]
    if missing_completed:
        raise ValueError(
            f"重规划不能删除已完成步骤：{', '.join(missing_completed)}"
        )

    contents = [step.content for step in parsed_steps]
    if len(contents) != len(set(contents)):
        raise ValueError("执行步骤中不能包含重复的 content")
    return parsed_steps


def _validate_status(status: PlanStatus, steps: list[PlanStep]) -> None:
    """校验 Plan 整体状态和步骤状态之间的不变量。"""
    # 1. 校验整体状态是否合法。
    valid_statuses: set[PlanStatus] = {"pending", "in_progress", "completed", "blocked"}
    if status not in valid_statuses:
        raise ValueError(f"Plan 的 status 无效：{status}")

    # 2. 根据整体状态约束进行中的步骤数量和完成情况。
    in_progress_count = sum(step.status == "in_progress" for step in steps)
    if status == "in_progress" and in_progress_count != 1:
        raise ValueError("Plan 为 in_progress 时，必须恰好有一个进行中的步骤")
    if status in {"pending", "blocked"} and in_progress_count != 0:
        raise ValueError(f"Plan 为 {status} 时，不能包含进行中的步骤")
    if status == "completed" and any(step.status != "completed" for step in steps):
        raise ValueError("Plan 为 completed 时，所有步骤都必须已完成")


def _validate_blocked_reason(
    status: PlanStatus,
    blocked_reason: str | None,
) -> str | None:
    """校验并规范化 Plan 的结构化阻塞原因。"""
    if status == "blocked":
        if not isinstance(blocked_reason, str) or not blocked_reason.strip():
            raise ValueError("Plan 为 blocked 时必须提供 blocked_reason")
        return blocked_reason.strip()
    if blocked_reason is not None:
        raise ValueError("Plan 非 blocked 状态时 blocked_reason 必须为 null")
    return None


def _normalize_replan_reason(
    current_plan: Plan,
    updated_steps: list[PlanStep],
    reason: str | None,
) -> str | None:
    """仅在计划结构发生变化时保留并要求重规划原因。"""
    current_step_ids = [step.step_id for step in current_plan.steps]
    updated_step_ids = [step.step_id for step in updated_steps]
    plan_changed = current_step_ids != updated_step_ids
    if not plan_changed:
        return None
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Plan 结构发生变化时必须提供重规划 reason")
    return reason.strip()


def validate_create_plan(arguments: dict[str, Any], context: ToolExecutionContext) -> str | None:
    """返回创建 Plan 的参数错误；参数合法时返回空值。"""
    try:
        objective = arguments["objective"]
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("Plan 目标不能为空")
        if context.plan_store.get_plan() is not None:
            raise ValueError("当前已有 Plan，不能重复创建；请更新现有 Plan")
        steps = _parse_steps(arguments["steps"])
        _validate_status("in_progress", steps)
    except (KeyError, TypeError, ValueError) as error:
        return str(error)
    return None


def create_plan(
    objective: str,
    steps: list[dict[str, Any]],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """根据用户整体目标创建一个 Plan。"""
    # 1. 解析步骤并建立固定目标的 Plan。
    parsed_steps = _parse_steps(steps)
    _validate_status("in_progress", parsed_steps)
    plan = Plan(objective.strip(), "in_progress", parsed_steps)

    # 2. 保存 Plan 并返回创建结果。
    context.plan_store.set_plan(plan)
    return ToolHandlerResult.success(
        f"Plan 已创建：{plan.objective}，共 {len(plan.steps)} 个步骤",
        {
            "status": plan.status,
            "revision": plan.revision,
            "step_count": len(plan.steps),
            "completed_count": 0,
        },
    )


def validate_update_plan(arguments: dict[str, Any], context: ToolExecutionContext) -> str | None:
    """返回更新 Plan 的参数错误；参数合法时返回空值。"""
    try:
        current_plan = context.plan_store.get_plan()
        if current_plan is None:
            raise ValueError("当前没有 Plan，请先调用 create_plan")
        expected_revision = arguments["expected_revision"]
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise ValueError("expected_revision 必须是整数")
        if expected_revision != current_plan.revision:
            raise ValueError(
                f"Plan 版本冲突：当前 revision 为 {current_plan.revision}，"
                f"收到 {expected_revision}"
            )
        steps = _parse_updated_steps(arguments["steps"], current_plan)
        _validate_status(arguments["status"], steps)
        _validate_blocked_reason(arguments["status"], arguments["blocked_reason"])
        _normalize_replan_reason(current_plan, steps, arguments["reason"])
    except (KeyError, TypeError, ValueError) as error:
        return str(error)
    return None


def update_plan(
    expected_revision: int,
    status: PlanStatus,
    reason: str | None,
    blocked_reason: str | None,
    steps: list[dict[str, Any]],
    context: ToolExecutionContext,
) -> ToolHandlerResult:
    """在保持目标不变的前提下更新 Plan 状态和执行步骤。"""
    # 1. 读取当前 Plan 并解析新的完整步骤计划。
    current_plan = context.plan_store.get_plan()
    if current_plan is None:
        raise ValueError("当前没有 Plan，请先调用 create_plan")
    if expected_revision != current_plan.revision:
        raise ValueError(
            f"Plan 版本冲突：当前 revision 为 {current_plan.revision}，"
            f"收到 {expected_revision}"
        )
    parsed_steps = _parse_updated_steps(steps, current_plan)
    _validate_status(status, parsed_steps)
    normalized_blocked_reason = _validate_blocked_reason(status, blocked_reason)
    normalized_replan_reason = _normalize_replan_reason(
        current_plan,
        parsed_steps,
        reason,
    )

    # 2. 保留原始目标，原子替换整体状态和步骤。
    next_revision = current_plan.revision + 1
    context.plan_store.set_plan(
        Plan(
            current_plan.objective,
            status,
            parsed_steps,
            next_revision,
            normalized_replan_reason,
            normalized_blocked_reason,
        )
    )
    completed_count = sum(step.status == "completed" for step in parsed_steps)
    return ToolHandlerResult.success(
        (
            f"Plan 已更新至 revision {next_revision}：状态 {status}，"
            f"已完成 {completed_count}/{len(parsed_steps)} 个步骤"
        ),
        {
            "status": status,
            "revision": next_revision,
            "step_count": len(parsed_steps),
            "completed_count": completed_count,
        },
    )
