"""把 Session 事实投影为 Web 刷新所需的只读视图。"""

from typing import Any


def transcript_messages(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从完整 Transcript 中提取用户与 Assistant 可见消息。"""
    messages: list[dict[str, Any]] = []
    for record in records:
        role, content = _visible_message(record)
        if role is None or not content:
            continue
        messages.append(
            {
                "sequence": record["sequence"],
                "role": role,
                "content": content,
                "created_at": record["created_at"],
            }
        )
    return messages


def _visible_message(record: dict[str, Any]) -> tuple[str | None, str]:
    """判断一条 Transcript 事实是否属于用户可见对话。"""
    record_type = record.get("type")
    payload = record.get("payload")
    if record_type in {"user_query", "steering_query_applied"}:
        if isinstance(payload, dict) and isinstance(payload.get("content"), str):
            return "user", payload["content"]
    if record_type == "message":
        return "assistant", _assistant_text(payload)
    return None, ""


def _assistant_text(payload: Any) -> str:
    """从 Responses API Assistant message 中提取文本内容。"""
    if not isinstance(payload, dict) or payload.get("role") != "assistant":
        return ""
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") in {"output_text", "text"}
        and isinstance(item.get("text"), str)
    )
