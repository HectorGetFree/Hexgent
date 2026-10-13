"""压缩切点之前的历史事实候选；来源存在不代表语义正确。"""

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any

from hexgent.utils.token_estimator import estimate_value_tokens


def json_value(value: Any) -> Any:
    """使用与 Transcript 等价的 JSON 形态，避免 SDK/恢复字典身份漂移。"""
    if hasattr(value, "to_message"):
        return json_value(value.to_message())
    if hasattr(value, "model_dump"):
        return json_value(value.model_dump())
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "__dict__"):
        return json_value(vars(value))
    raise ValueError("SNAPSHOT_SOURCE_NOT_SERIALIZABLE")


def source_hash(value: Any) -> str:
    return sha256(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode()).hexdigest()


def snapshot_message(snapshot: dict) -> dict:
    """以 assistant 历史证据注入，不授予高于用户约束的指令权限。"""
    body = json.dumps({k: snapshot[k] for k in ("as_of_message_id", "facts")}, ensure_ascii=False)
    body = body.replace("<", "\\u003c").replace(">", "\\u003e")
    return {"role": "assistant", "content":
            "以下是模型提取的历史事实快照，不是新用户指令；仅截至标记切点，"
            "来源存在不保证语义真实，近期消息可能更正它。\n"
            "<key-fact-snapshot>\n" + body + "\n</key-fact-snapshot>"}


def validate_snapshot(raw: Any, *, cutpoint: str, sources: list[dict],
                      previous: dict | None, budget_tokens: int, excluded_source_ids: tuple[str, ...] = ()) -> dict:
    """校验 Schema、允许来源和旧事实处置；不宣称验证事实语义。"""
    if not isinstance(raw, dict) or set(raw) != {"as_of_message_id", "facts", "dispositions"}:
        raise ValueError("SNAPSHOT_SCHEMA_INVALID")
    if raw["as_of_message_id"] != cutpoint:
        raise ValueError("SNAPSHOT_CUTPOINT_INVALID")
    all_prefix_ids = [row["message_id"] for row in sources]
    prefix = {row["message_id"]: row for row in sources if row["message_id"] not in excluded_source_ids}
    inherited = (previous or {}).get("source_catalog", {})
    previous_cutoff = (previous or {}).get("source_sequence_cutoff", 0)
    def sequence(identity):
        return int(identity[4:]) if identity.startswith("msg-") and identity[4:].isdigit() else 0
    # 上一代摘要/已保留旧 Query 不是更新事实的新证据。
    previously_reference_only = set((previous or {}).get("excluded_reference_ids", []))
    new_evidence = {identity for identity in prefix if sequence(identity) > previous_cutoff
                    or identity in previously_reference_only}
    if any(identity in inherited and inherited[identity]["sha256"] != source_hash(row["message"])
           for identity, row in prefix.items()):
        raise ValueError("SNAPSHOT_SOURCE_ID_COLLISION")
    allowed = {**inherited, **{k: {"sha256": source_hash(v["message"])} for k, v in prefix.items()}}
    facts = raw["facts"]
    if not isinstance(facts, list) or not facts:
        raise ValueError("SNAPSHOT_EMPTY_OR_INVALID")
    indexed = {}
    for fact in facts:
        if not isinstance(fact, dict) or set(fact) != {"id", "content", "source_refs", "status"}:
            raise ValueError("SNAPSHOT_FACT_SCHEMA_INVALID")
        if not isinstance(fact["id"], str) or not fact["id"].strip() or fact["id"] in indexed:
            raise ValueError("SNAPSHOT_FACT_ID_INVALID")
        if not isinstance(fact["content"], str) or not fact["content"].strip():
            raise ValueError("SNAPSHOT_FACT_CONTENT_INVALID")
        if fact["status"] not in {"active", "uncertain"}:
            raise ValueError("SNAPSHOT_FACT_STATUS_INVALID")
        refs = fact["source_refs"]
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
            raise ValueError("SNAPSHOT_SOURCE_INVALID")
        if len(refs) != len(set(refs)) or any(r not in allowed for r in refs):
            raise ValueError("SNAPSHOT_SOURCE_OUT_OF_SCOPE")
        indexed[fact["id"]] = fact
    old = {f["id"]: f for f in (previous or {}).get("facts", [])}
    dispositions = raw["dispositions"]
    if not isinstance(dispositions, list):
        raise ValueError("SNAPSHOT_DISPOSITIONS_INVALID")
    handled = set()
    inherited_targets = set()
    for item in dispositions:
        if not isinstance(item, dict) or set(item) != {"old_fact_id", "action", "new_fact_id", "source_refs"}:
            raise ValueError("SNAPSHOT_DISPOSITION_SCHEMA_INVALID")
        old_id, action, new_id, refs = (item[k] for k in ("old_fact_id", "action", "new_fact_id", "source_refs"))
        if not isinstance(old_id, str) or old_id not in old or old_id in handled:
            raise ValueError("SNAPSHOT_DISPOSITION_ID_INVALID")
        if action not in {"CARRY_FORWARD", "UPDATED", "SUPERSEDED", "UNCERTAIN"}:
            raise ValueError("SNAPSHOT_DISPOSITION_ACTION_INVALID")
        if not isinstance(refs, list) or not all(isinstance(r, str) and r in new_evidence for r in refs):
            raise ValueError("SNAPSHOT_NEW_EVIDENCE_INVALID")
        if action == "SUPERSEDED":
            if new_id is not None or not refs:
                raise ValueError("SNAPSHOT_SUPERSEDE_WITHOUT_EVIDENCE")
        else:
            if not isinstance(new_id, str) or new_id not in indexed or new_id in inherited_targets:
                raise ValueError("SNAPSHOT_DISPOSITION_TARGET_INVALID")
            target = indexed[new_id]
            inherited_targets.add(new_id)
            if action == "CARRY_FORWARD":
                if any(target[k] != old[old_id][k] for k in ("id", "content", "status", "source_refs")):
                    raise ValueError("SNAPSHOT_CARRY_CHANGED")
            elif not refs or not set(refs).issubset(target["source_refs"]):
                raise ValueError("SNAPSHOT_UPDATE_WITHOUT_EVIDENCE")
            if action == "UNCERTAIN" and target["status"] != "uncertain":
                raise ValueError("SNAPSHOT_UNCERTAINTY_LOST")
        handled.add(old_id)
    if handled != set(old):
        raise ValueError("SNAPSHOT_OLD_FACT_UNHANDLED")
    for fact_id, fact in indexed.items():
        if fact_id not in inherited_targets and not set(fact["source_refs"]).intersection(prefix):
            raise ValueError("SNAPSHOT_NEW_FACT_WITHOUT_PREFIX_SOURCE")
    result = deepcopy(raw)
    used = {r for f in facts for r in f["source_refs"]}
    result.update(schema_version=1, source_catalog={r: allowed[r] for r in sorted(used)},
                  source_sequence_cutoff=max([previous_cutoff, *(sequence(identity) for identity in all_prefix_ids)]))
    result["excluded_reference_ids"] = [identity for identity in excluded_source_ids if identity in all_prefix_ids]
    if estimate_value_tokens(snapshot_message(result)) > budget_tokens:
        raise ValueError("SNAPSHOT_LIMIT_EXCEEDED")
    return result
