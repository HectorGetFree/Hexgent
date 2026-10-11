"""用户显式管理的 Session 约束；完整 Transcript 是唯一恢复权威。"""
from dataclasses import asdict, dataclass, replace
from uuid import uuid4


@dataclass(frozen=True)
class PinnedConstraint:
    id: str
    source_seq: int
    original_text: str
    status: str = "ACTIVE"
    superseded_by: str | None = None
    version: int = 1


def replay_constraints(records):
    state, commands = {}, {}
    for event in records:
        if event["type"] != "pinned_constraint_committed":
            continue
        payload = event["payload"]
        for item in payload["changes"]:
            constraint = PinnedConstraint(**item)
            state[constraint.id] = constraint
        commands[payload["command_id"]] = payload
    return state, commands


def active_constraints(store):
    if store is None or store.session_id is None:
        return ()
    with store.write_guard():
        state, _ = replay_constraints(store.read_transcript())
        return tuple(asdict(item) for item in state.values() if item.status == "ACTIVE")


def manage_constraint(store, action, command_id, text=None, constraint_id=None,
                      expected_version=None, source_seq=None):
    if action not in {"pin", "replace", "revoke"}:
        raise ValueError("CONSTRAINT_ACTION_INVALID")
    if not isinstance(command_id, str) or not command_id.strip():
        raise ValueError("COMMAND_ID_REQUIRED")
    if action != "revoke" and (not isinstance(text, str) or not text.strip()):
        raise ValueError("CONSTRAINT_TEXT_REQUIRED")
    if store is None:
        raise RuntimeError("PINNED_REQUIRES_DURABLE_SESSION")
    if store.session_id is None:
        store.start_named_session("pinned-" + uuid4().hex, "Pinned Session")
    intent = dict(action=action,text=text,constraint_id=constraint_id,
                  expected_version=expected_version,source_seq=source_seq)
    with store.write_guard():
        records = store.read_transcript()
        state, commands = replay_constraints(records)
        if command_id in commands:
            previous = commands[command_id]
            if previous["intent"] != intent:
                raise ValueError("IDEMPOTENCY_CONFLICT")
            return previous
        # 1. 历史来源必须是本 Session 已落盘的用户原文；工具和模型事件不能升级。
        if source_seq is not None:
            source = next((event for event in records if event["sequence"] == source_seq), None)
            if (type(source_seq) is not int or source is None or
                    source["type"] != "context_user_query_appended" or
                    source["payload"].get("source", "user") != "user" or
                    source["payload"].get("content") != text):
                raise ValueError("CONSTRAINT_SOURCE_INVALID")
        changes = []
        old = state.get(constraint_id)
        if action != "pin":
            if old is None:
                raise ValueError("CONSTRAINT_NOT_FOUND")
            if type(expected_version) is not int or old.version != expected_version:
                raise ValueError("CONSTRAINT_VERSION_CONFLICT")
            if old.status != "ACTIVE":
                if action == "revoke" and old.status == "REVOKED":
                    # 2. 无状态变化的成功命令也必须落盘，才能重放其幂等身份。
                    payload = dict(command_id=command_id,intent=intent,changes=[],
                        source_kind="USER_TRANSCRIPT" if source_seq is not None else "EXPLICIT_USER_COMMAND")
                    store.append_transcript("pinned_constraint_committed",payload)
                    return payload
                raise ValueError("CONSTRAINT_NOT_ACTIVE")
        new = None
        if action != "revoke":
            new = PinnedConstraint("pc-" + uuid4().hex, source_seq or store.next_sequence, text)
        if old is not None:
            changes.append(asdict(replace(old,status="SUPERSEDED" if new else "REVOKED",
                superseded_by=new.id if new else None,version=old.version+1)))
        if new is not None:
            changes.append(asdict(new))
        payload = dict(command_id=command_id,intent=intent,changes=changes,
                       source_kind="USER_TRANSCRIPT" if source_seq is not None else "EXPLICIT_USER_COMMAND")
        # 2. Replace 的旧失效和新生效写在同一行，不依赖多个投影提交。
        store.append_transcript("pinned_constraint_committed",payload)
        return payload
