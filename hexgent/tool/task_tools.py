"""创建 Task 和动态维护执行计划的工具。"""

from typing import Any

from hexgent.task import Task, TaskStatus, TaskStep, TaskStepStatus
from hexgent.tool.tool_context import ToolExecutionContext


def _parse_steps(steps: list[dict[str, Any]]) -> list[TaskStep]:
    """将工具参数解析为严格的 TaskStep 列表。"""
    # 1. 校验列表和每个步骤的字段结构。
    if not isinstance(steps, list) or not steps:
        raise ValueError("Task 至少需要一个执行步骤")
    allowed_fields = {"content", "process_message", "status"}
    valid_statuses: set[TaskStepStatus] = {"pending", "in_progress", "completed"}
    parsed_steps: list[TaskStep] = []
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
        parsed_steps.append(TaskStep(content.strip(), process_message.strip(), status))

    # 2. 拒绝内容相同的重复步骤。
    contents = [step.content for step in parsed_steps]
    if len(contents) != len(set(contents)):
        raise ValueError("执行步骤中不能包含重复的 content")
    return parsed_steps


def _validate_status(status: TaskStatus, steps: list[TaskStep]) -> None:
    """校验 Task 整体状态和步骤状态之间的不变量。"""
    # 1. 校验整体状态是否合法。
    valid_statuses: set[TaskStatus] = {"pending", "in_progress", "completed", "blocked"}
    if status not in valid_statuses:
        raise ValueError(f"Task 的 status 无效：{status}")

    # 2. 根据整体状态约束进行中的步骤数量和完成情况。
    in_progress_count = sum(step.status == "in_progress" for step in steps)
    if status == "in_progress" and in_progress_count != 1:
        raise ValueError("Task 为 in_progress 时，必须恰好有一个进行中的步骤")
    if status in {"pending", "blocked"} and in_progress_count != 0:
        raise ValueError(f"Task 为 {status} 时，不能包含进行中的步骤")
    if status == "completed" and any(step.status != "completed" for step in steps):
        raise ValueError("Task 为 completed 时，所有步骤都必须已完成")


def validate_create_task(arguments: dict[str, Any], context: ToolExecutionContext) -> str | None:
    """返回创建 Task 的参数错误；参数合法时返回空值。"""
    try:
        objective = arguments["objective"]
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("Task 目标不能为空")
        if context.task_store.get_task() is not None:
            raise ValueError("当前已有 Task，不能重复创建；请更新现有 Task 计划")
        steps = _parse_steps(arguments["steps"])
        _validate_status("in_progress", steps)
    except (KeyError, TypeError, ValueError) as error:
        return str(error)
    return None


def create_task(objective: str, steps: list[dict[str, Any]], context: ToolExecutionContext) -> str:
    """根据用户整体目标创建一个 Task。"""
    # 1. 解析步骤并建立固定目标的 Task。
    parsed_steps = _parse_steps(steps)
    _validate_status("in_progress", parsed_steps)
    task = Task(objective.strip(), "in_progress", parsed_steps)

    # 2. 保存 Task 并返回创建结果。
    context.task_store.set_task(task)
    return f"Task 已创建：{task.objective}，共 {len(task.steps)} 个步骤"


def validate_update_task_plan(arguments: dict[str, Any], context: ToolExecutionContext) -> str | None:
    """返回更新 Task 计划的参数错误；参数合法时返回空值。"""
    try:
        if context.task_store.get_task() is None:
            raise ValueError("当前没有 Task，请先调用 create_task")
        steps = _parse_steps(arguments["steps"])
        _validate_status(arguments["status"], steps)
    except (KeyError, TypeError, ValueError) as error:
        return str(error)
    return None


def update_task_plan(
    status: TaskStatus,
    steps: list[dict[str, Any]],
    context: ToolExecutionContext,
) -> str:
    """在保持目标不变的前提下更新 Task 状态和执行步骤。"""
    # 1. 读取当前 Task 并解析新的完整步骤计划。
    current_task = context.task_store.get_task()
    if current_task is None:
        raise ValueError("当前没有 Task，请先调用 create_task")
    parsed_steps = _parse_steps(steps)
    _validate_status(status, parsed_steps)

    # 2. 保留原始目标，原子替换整体状态和步骤。
    context.task_store.set_task(Task(current_task.objective, status, parsed_steps))
    completed_count = sum(step.status == "completed" for step in parsed_steps)
    return f"Task 计划已更新：状态 {status}，已完成 {completed_count}/{len(parsed_steps)} 个步骤"
