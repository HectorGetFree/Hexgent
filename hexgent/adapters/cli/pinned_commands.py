"""显式 CLI 管理命令；用户输入由 Adapter 消费，不进入模型工具协议。"""
import json
from uuid import uuid4


def handle_pinned_command(engine, query, emit=None):
    emit = emit or print
    parts = query.strip().split(None, 1)
    command = parts[0] if parts else ""
    remaining = parts[1] if len(parts) > 1 else ""
    if command not in {"/pin", "/replace-pin", "/revoke-pin", "/pins"}:
        return False
    try:
        if command == "/pins":
            if remaining.strip():
                raise ValueError("用法：/pins")
            result = engine.list_pinned_constraints()
        elif command == "/pin":
            result = engine.manage_pinned_constraint("pin", uuid4().hex, text=remaining)
        else:
            parts = remaining.split(None, 2)
            if len(parts) != (3 if command == "/replace-pin" else 2):
                raise ValueError("用法：/replace-pin <id> <version> <原文> 或 /revoke-pin <id> <version>")
            result = engine.manage_pinned_constraint("replace" if command == "/replace-pin" else "revoke",
                uuid4().hex, constraint_id=parts[0], expected_version=int(parts[1]),
                **({"text": parts[2]} if command == "/replace-pin" else {}))
        emit(json.dumps(result, ensure_ascii=False))
    except (ValueError, RuntimeError, OSError) as error:
        emit("约束管理失败：" + str(error))
    return True
