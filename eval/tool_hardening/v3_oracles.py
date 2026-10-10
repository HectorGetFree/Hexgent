"""工具执行、模型结果交付与任务证据独立判定。"""

import json
import re


def layered_oracles(records, turns, evidence, workspace=None):
    """以 durable 事实与实际下一轮请求交叉核验，不以引用质量代替工具正确性。"""
    main = [t for t in turns if t["request_type"] == "MAIN_AGENT"]
    calls = [c for t in main for c in t["tool_calls"]]
    finished = [r["payload"] for r in records if r["type"] == "tool_execution_finished"]
    dispatched = [r["payload"]["call_id"] for r in records if r["type"] == "tool_execution_dispatched"]
    starts = [r["payload"] for r in records if r["type"] == "tool_execution_started"]
    execution_errors, delivery_errors, blocked = [], [], []
    ids = [c["call_id"] for c in calls]
    if len(ids) != len(set(ids)):
        execution_errors.append("DUPLICATE_CALL_ID")
    durable_outputs = {}
    for record in records:
        if record["type"] == "context_messages_appended":
            for message in record["payload"].get("messages", []):
                if message.get("type") == "function_call_output":
                    durable_outputs.setdefault(message["call_id"], []).append(message["output"])
    checks = []
    for call in calls:
        cid = call["call_id"]
        rows = [p for p in finished if p["call_id"] == cid]
        if len(rows) != 1 or rows[0].get("status") != "success" or dispatched.count(cid) != 1:
            execution_errors.append(f"EXECUTION_FACT_MISMATCH:{cid}")
        if workspace is not None:
            declared = [p for p in starts if p["call_id"] == cid]
            if len(declared) != 1 or declared[0].get("tool_name") != call.get("name"):
                execution_errors.append(f"CALL_IDENTITY_MISMATCH:{cid}")
            else:
                def arguments(value):
                    return json.loads(value) if isinstance(value, str) else value
                if arguments(declared[0].get("arguments")) != arguments(call.get("arguments")):
                    execution_errors.append(f"ARGUMENT_MISMATCH:{cid}")
        # 真实 permission allow 必须在 dispatch 前出现。
        before = []
        for r in records:
            if r["type"] == "tool_execution_dispatched" and r["payload"].get("call_id") == cid:
                break
            if r["payload"].get("call_id") == cid and r["type"] in {"permission_decided", "permission_resolved"}:
                before.append(r["payload"])
        if not any(p.get("behavior") == "allow" for p in before):
            execution_errors.append(f"NO_PRIOR_ALLOW:{cid}")
    for index, turn in enumerate(main):
        for call in turn["tool_calls"]:
            cid = call["call_id"]
            if index + 1 == len(main):
                blocked.append(f"NO_NEXT_MODEL_REQUEST:{cid}")
                continue
            inputs = main[index + 1]["request"].get("input", [])
            outputs = [m.get("output") for m in inputs if isinstance(m, dict) and m.get("type") == "function_call_output" and m.get("call_id") == cid]
            expected = durable_outputs.get(cid, [])
            ok = len(outputs) == len(expected) == 1 and outputs == expected
            finished_verified = None
            if workspace is not None and len(expected) == 1:
                fact = next((p for p in finished if p["call_id"] == cid), {})
                output = expected[0]
                match = re.match(r'<persisted-output\npath="([^"]+)"\noriginal_characters="(\d+)">\n', output)
                if match:
                    root = workspace.resolve()
                    path = (root / match[1]).resolve()
                    try:
                        path.relative_to(root)
                        full = path.read_text()
                        preview = output[match.end():].split("\n</persisted-output>", 1)[0]
                        finished_verified = (len(full) == int(match[2]) and
                                             (fact.get("persisted_output") == match[1] or fact.get("output") == full) and
                                             (preview in full or preview.startswith(full[:100])))
                    except (ValueError, OSError):
                        finished_verified = False
                else:
                    finished_verified = fact.get("output") == output and not fact.get("persisted_output")
                if not finished_verified:
                    delivery_errors.append(f"FINISHED_RESULT_MISMATCH:{cid}")
            checks.append({"call_id": cid, "exact_durable_context_delivery": ok, "finished_result_verified": finished_verified})
            if not ok:
                delivery_errors.append(f"RESULT_PAIR_OR_CONTENT_MISMATCH:{cid}")
    if set(dispatched) - set(ids) or {r["call_id"] for r in finished} - set(ids):
        execution_errors.append("UNOBSERVED_CALL")
    return {"tool_execution": {"status": "FAIL" if execution_errors else "PASS" if calls else "BLOCKED", "errors": execution_errors, "call_count": len(calls)},
            "tool_result_delivery": {"status": "FAIL" if delivery_errors else "BLOCKED" if blocked or not checks else "PASS", "errors": delivery_errors, "blocked": blocked, "checks": checks,
                                     "scope": "durable Finished inline/persisted result, Context output and actual next main request; not model semantic understanding"},
            "task_evidence": {"status": "PASS" if evidence["mechanical_pass"] else "FAIL", **evidence}}
