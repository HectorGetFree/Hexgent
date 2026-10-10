"""M1 显式 Memory ON/OFF：仅 Eval，复用生产 Engine、Store 和权限管线。"""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shutil

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig, load_config
from hexgent.memory import MemoryStore
from hexgent.observation import source_fingerprint
from hexgent.session.checkpoint import CheckpointStore
from hexgent.tool.tool_permission_pipeline import PermissionConfig, PermissionContext, evaluate_permission
from hexgent.tool.tool_registry import get_tool


CASES = (
    dict(id="M1-01", category="preference", entity="Veyra-K8L",
         fact="Veyra-K8L 的评审标题顺序固定为：边界先述、反例核对、落地清单。",
         question="Veyra-K8L 的评审标题按什么顺序排列？value 使用字符串数组。",
         expected=["边界先述", "反例核对", "落地清单"], anchors=["Veyra-K8L", "边界先述", "反例核对", "落地清单"]),
    dict(id="M1-02", category="constraint", entity="Zuno-R4Q",
         fact="Zuno-R4Q 的发布通知只能发送到青铜工单-73，禁止使用邮件。",
         question="Zuno-R4Q 的发布通知应使用哪个渠道？value 使用渠道字符串。",
         expected="青铜工单-73", anchors=["Zuno-R4Q", "青铜工单-73", "禁止", "邮件"]),
    dict(id="M1-03", category="project", entity="Kestrel-T9N",
         fact="Kestrel-T9N 的服务端口固定为 48271，压缩编码固定为 lz4-block-v2。",
         question='Kestrel-T9N 的服务端口和压缩编码是什么？value 使用 {"port":整数,"codec":字符串}。',
         expected=dict(port=48271, codec="lz4-block-v2"), anchors=["Kestrel-T9N", "48271", "lz4-block-v2"]),
    dict(id="M1-04", category="project", entity="Maru-N6S",
         fact="Maru-N6S 的突发额度为 731 次，计数窗口为 19 秒。",
         question='Maru-N6S 的突发额度和计数窗口分别是多少？value 使用 {"burst":整数,"window_seconds":整数}。',
         expected=dict(burst=731, window_seconds=19), anchors=["Maru-N6S", "731", "19"]),
    dict(id="M1-05", category="constraint", entity="Orin-V2D",
         fact="Orin-V2D 仅在区域为岚岛且告警等级至少为4时，转交银组-58；其他情况均转交观察队-26。",
         question="Orin-V2D 中，岚岛等级5、岚岛等级2、洲桥等级5分别应转交谁？value 按这三个条件的顺序使用字符串数组。",
         expected=["银组-58", "观察队-26", "观察队-26"], anchors=["Orin-V2D", "岚岛", "4", "银组-58", "观察队-26"]),
    dict(id="M1-06", category="project", entity="Luma-A7G",
         fact="Luma-A7C 的发布标签为 amber-41；Luma-A7G 的发布标签为 navy-86。这两个实体不得混用。",
         question="Luma-A7G 的发布标签是什么？value 使用标签字符串。",
         expected="navy-86", anchors=["Luma-A7C", "amber-41", "Luma-A7G", "navy-86"]),
)


def dump(path, value):
    """原始证据只新增；禁止覆盖或自动重试同一实验。"""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def tree_hash(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}


def queries(case):
    return (case["fact"] + " 请为后续会话长期记住这项信息，完整保留这段事实原文，不改写实体、具体取值及关联关系。",
            case["question"] + ' 仅根据已知的该实体事实作答；没有依据时不要猜测。只返回 JSON {"value":答案}，不知道则 value=null。')


def prepare(root):
    root = root.resolve()
    if root.exists():
        raise ValueError("M1 输出目录已存在；不得覆盖已注册样本")
    repo = Path(__file__).resolve().parents[1]
    config = replace(load_config(repo), permission=PermissionConfig(default_mode="dont_ask"))
    root.mkdir(parents=True)
    cases = []
    for index, original in enumerate(CASES):
        case = {**original, "order": ["ON", "OFF"] if index % 2 == 0 else ["OFF", "ON"]}
        case["write_query"], case["query"] = queries(case)
        cases.append(case)
        (root / case["id"]).mkdir()
    dump(root / "manifest.json", dict(revision="memory-m1-v1", runtime_sha256=source_fingerprint(),
         cases=cases, base_config=asdict(config), config_source=str(repo / "hexgent.toml"),
         scope_profile="eval_private_memory_store_production_dont_ask_v1",
         model_protocol="production Responses streaming; no provider proxy",
         planned_cases=6, planned_sessions=18,
         limitations=["六例单次配对 Smoke，不是广泛稳定性或效率基准", "显式 remember，不评测自动沉淀",
                      "Eval 私有 Store 注入，不是默认 CLI 文件布局", "工作区权限不是 OS sandbox",
                      "ON/OFF 共用 dont_ask；仅 OFF 的 memory_retrieval.enabled 不同",
                      "写入 Oracle 使用文本锚点；合法语义改写可能需要人工复核"]))
    print(json.dumps({"manifest": str(root / "manifest.json")}, ensure_ascii=False))


def config_for(manifest, variant):
    # 1. 复用冻结的配置，不向生产新增开关；ON/OFF 仅有一个字段差异。
    from hexgent.config import _load_section
    base = HexgentConfig()
    config = HexgentConfig(**{name: _load_section(name, payload, type(getattr(base, name)))
                            for name, payload in manifest["base_config"].items()})
    return replace(config, memory_retrieval=replace(config.memory_retrieval, enabled=variant != "OFF"))


def scope_probes(engine, private_root):
    """执行前核验真实权限决策，不执行任何危险 Handler。"""
    probes = [
        ("read_file", {"path": str(private_root / ".hexgent/memory/MEMORY.md")}, "deny"),
        ("list_directory", {"path": str(private_root)}, "deny"),
        ("glob", {"pattern": "../**"}, "deny"),
        ("bash", {"command": "cat ../vault/.hexgent/memory/MEMORY.md"}, "deny"),
        ("read_file", {"path": "pyproject.toml"}, "allow"),
    ]
    results = []
    for name, args, expected in probes:
        result = evaluate_permission(name, get_tool(name), args, engine.tool_context,
                                     PermissionContext("dont_ask"))
        results.append(dict(tool=name, arguments=args, behavior=result.behavior,
                            rule=result.rule_id, expected=expected))
    if any(p["behavior"] != p["expected"] for p in results):
        raise RuntimeError("SCOPE_ISOLATION_PROBE_FAILED")
    return results


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def collect(engine):
    """基于权威 Transcript、已校验 Checkpoint 和请求观测独立投影。"""
    session = engine.session.store.session_dir
    transcript = rows(session / "transcript.jsonl")
    observations = rows(session / "observation.jsonl") if (session / "observation.jsonl").exists() else []
    payloads = lambda kind: [r["payload"] for r in transcript if r["type"] == kind]
    finished = payloads("agent_run_finished")
    terminal = finished[-1] if finished else {}
    checkpoints = payloads("checkpoint_committed")
    state = None
    if checkpoints:
        checkpoint = CheckpointStore(session).read(checkpoints[-1]["checkpoint_id"])
        if checkpoint.reason == "run_finished":
            state = checkpoint.run_state
    starts = [r for r in observations if r["event"] == "provider_started"]
    ends = [r for r in observations if r["event"] == "provider_finished"]
    complete_usage = bool(starts) and len(starts) == len(ends) and all(r.get("usage_available") for r in ends)
    successful = {r["span_id"] for r in ends if r["status"] == "SUCCEEDED"}
    receipts = [r for r in observations if r["event"] == "memory_injected"
                and r["request_type"] == "MAIN_AGENT" and r["span_id"] in successful]
    return dict(session=str(session), session_id=engine.session.store.session_id,
                terminal_status=terminal.get("status"), final_answer=terminal.get("output_text"),
                provider_requests=len(starts), provider_request_events=starts + ends,
                provider_tokens=dict(input=sum(r["input_tokens"] for r in ends) if complete_usage else None,
                                     output=sum(r["output_tokens"] for r in ends) if complete_usage else None,
                                     observed_input_subtotal=sum(r.get("input_tokens") or 0 for r in ends),
                                     observed_output_subtotal=sum(r.get("output_tokens") or 0 for r in ends)),
                usage_completeness="COMPLETE" if complete_usage else "INCOMPLETE",
                provider_failures=[r for r in ends if r["status"] != "SUCCEEDED"],
                provider_observation_complete=bool(starts) and {r["span_id"] for r in starts} == {r["span_id"] for r in ends},
                missing_usage=[dict(span_id=r["span_id"], status=r["status"],
                                   reason="PROVIDER_DID_NOT_REPORT_USAGE" if r["status"] == "SUCCEEDED" else "UNSUCCESSFUL_REQUEST_USAGE_UNAVAILABLE")
                               for r in ends if not r.get("usage_available")],
                source_hashes=[r["runtime_sha256"] for r in observations if r["event"] == "source_snapshot"],
                total_agent_latency_ms=terminal.get("elapsed_seconds", 0) * 1000 if terminal else None,
                model_failures=payloads("model_response_failed"),
                tool_calls=payloads("tool_execution_finished"),
                tool_arguments={p["call_id"]: dict(name=p["tool_name"], arguments=p["arguments"])
                                for p in payloads("tool_execution_started")},
                retrieved_memory=state.get("relevant_memory") if state is not None else None,
                relevant_context=state.get("relevant_context") if state is not None else None,
                memory_receipts=receipts,
                actual_provider_models=sorted({(r.get("provider_host"), r.get("model"))
                                               for r in starts if r["request_type"] == "MAIN_AGENT"}, key=str))


def write_oracle(case, result, store):
    calls = [c for c in result["tool_calls"] if c["tool_name"] == "remember" and c["status"] == "success"]
    ids = {(c.get("data") or {}).get("memory_id") for c in calls}
    normalized_fact = "".join(case["fact"].split()).rstrip("。")
    gold = [item for item in store.items if item.memory_id in ids and item.source == "explicit"
            and normalized_fact in "".join(item.content.split())]
    # 明确要求保存原文；核对完整关联，不能只靠词袋匹配掩盖数值/实体绑定错误。
    return dict(status="PASS" if len(gold) == 1 and result["terminal_status"] == "completed" else "FAIL",
                tool_call_count=len(calls), facts=[asdict(item) for item in gold],
                stored_items=[asdict(item) for item in store.items])


def answer_oracle(case, text):
    try:
        parsed = json.loads(text or "")
        if not isinstance(parsed, dict) or set(parsed) != {"value"}:
            raise ValueError("答案 Schema 不匹配")
        value = parsed["value"]
        # 使用规范 JSON 比较，不能将 true 当成数值 1。
        correct = json.dumps(value, ensure_ascii=False, sort_keys=True) == json.dumps(case["expected"], ensure_ascii=False, sort_keys=True)
        return dict(parse_valid=True, value=value, answer_correct=correct, asserted=value is not None)
    except (ValueError, TypeError):
        return dict(parse_valid=False, value=None, answer_correct=False, asserted=None)


def no_external_fact_source(result):
    """保留其他 Agent 能力；允许可独立证明未返回目标事实的工具结果。"""
    for call in result["tool_calls"]:
        if call["status"] in {"denied", "invalid"} or call["tool_name"] in {"create_plan", "update_plan"}:
            continue
        invocation = result.get("tool_arguments", {}).get(call["call_id"])
        if not invocation or invocation["name"] != call["tool_name"]:
            return False
        try:
            args = json.loads(invocation["arguments"])
        except (ValueError, TypeError):
            return False
        if call["tool_name"] == "glob" and call["status"] == "success":
            pattern = Path(args.get("pattern", "")).expanduser()
            data = call.get("data") or {}
            if (not pattern.is_absolute() and ".." not in pattern.parts and
                data.get("match_count") == data.get("returned_count") == 0 and
                call.get("output") == "（没有匹配项）"):
                continue
        # 空白模板是事先已知的无事实 fixture；不允许读取任意内部 Session/Record 文件。
        if call["tool_name"] == "read_file" and call["status"] == "success":
            workspace = Path(result["session"]).parent.parent.parent
            target = Path(args.get("path", "")).expanduser()
            target = (target if target.is_absolute() else workspace / target).resolve()
            marker = workspace / "pyproject.toml"
            if target == marker.resolve() and marker.read_text() == "# M1 trusted empty workspace\n":
                continue
        return False
    return True


def query_oracles(case, variant, result, write, before, after, fresh, probes):
    gold = write["facts"]
    gold_ids = {f["memory_id"] for f in gold}
    retrieved = result["retrieved_memory"]
    retrieval_hit = bool(gold_ids) and retrieved is not None and gold_ids <= {m["memory_id"] for m in retrieved}
    first = result["memory_receipts"][0] if result["memory_receipts"] else None
    injected = first.get("memories", []) if first else []
    gold_hash = {f["memory_id"]: sha256(f["content"].encode()).hexdigest() for f in gold}
    exact = {m["memory_id"] for m in injected if gold_hash.get(m["memory_id"]) == m.get("content_sha256")}
    injection_success = bool(gold_ids) and exact >= gold_ids
    scope = (fresh and before == after and result["relevant_context"] == []
             and all(p["behavior"] == p["expected"] for p in probes)
             and no_external_fact_source(result))
    if variant == "OFF":
        scope = scope and retrieved == [] and first is not None and all(not r["memories"] for r in result["memory_receipts"])
    answer = answer_oracle(case, result["final_answer"])
    unsupported = (answer["asserted"] and (variant == "OFF" or not injection_success or not answer["answer_correct"])) if answer["asserted"] is not None else None
    return dict(retrieval_hit=retrieval_hit if retrieved is not None else None,
                retrieval_oracle="PASS" if (retrieval_hit if variant == "ON" else retrieved == []) else "FAIL",
                injection_success=injection_success if first is not None else None,
                injection_oracle="PASS" if (injection_success if variant == "ON" else first is not None and not injected) else "FAIL",
                injected_fact_id=sorted(exact), scope_isolation_oracle="PASS" if scope else "FAIL",
                answer_oracle=answer, answer_correct=answer["answer_correct"], unsupported_answer=unsupported)


def execute_stage(root, manifest, case, variant, write=None):
    stage = root / case["id"] / variant
    stage.mkdir()  # 原始样本存在即拒绝，禁止无记录重试。
    workspace, vault = stage / "workspace", stage / "vault"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text("# M1 trusted empty workspace\n", encoding="utf-8")
    if variant != "WRITE":
        shutil.copytree(root / case["id"] / "WRITE/vault", vault)
    store = MemoryStore(vault)
    before = tree_hash(vault)
    engine = AgentEngine(config_for(manifest, variant), workspace, memory_store=store)
    fresh = not engine.context_manager.active_messages and not engine.session.context_records.active_records
    probes = scope_probes(engine, vault)
    dump(stage / "precheck.json", dict(fresh=fresh, permission_probes=probes, memory_before=before,
                                      config=asdict(engine.config), scope_id=case["id"]))
    exception = None
    try:
        for event in engine.run(RunRequest.submit(case["write_query"] if variant == "WRITE" else case["query"])):
            pass  # 实际事件、工具及 Provider 均由生产 Runtime 记录。
    except Exception as error:
        exception = dict(type=type(error).__name__, message=str(error))
    result = collect(engine)
    result.update(case_id=case["id"], variant=variant, exception=exception,
                  evidence_type="REAL_MODEL_BEHAVIORAL_EVIDENCE", permission_probes=probes)
    result["source_snapshot_matches"] = result["source_hashes"] == [manifest["runtime_sha256"]]
    result["memory_snapshot"] = tree_hash(vault)
    if variant == "WRITE":
        result["write_oracle"] = write_oracle(case, result, MemoryStore(vault))
    else:
        result.update(query_oracles(case, variant, result, write["write_oracle"], before, tree_hash(vault), fresh, probes))
        if before != write["memory_snapshot"]:
            result["scope_isolation_oracle"] = "FAIL"
    dump(stage / "result.json", result)
    print(json.dumps(dict(case=case["id"], variant=variant, terminal=result["terminal_status"],
                         answer_correct=result.get("answer_correct"), result=str(stage / "result.json")), ensure_ascii=False), flush=True)
    return result


def pair_valid(pair):
    return (set(pair) == {"ON", "OFF"} and all(r["terminal_status"] == "completed"
            and not r["exception"] and r["scope_isolation_oracle"] == "PASS"
            and r["source_snapshot_matches"] and r["provider_observation_complete"]
            and r["answer_oracle"]["parse_valid"] for r in pair.values())
            and pair["ON"]["actual_provider_models"] == pair["OFF"]["actual_provider_models"])


def load_result(path):
    value = json.loads(path.read_text())
    correction = path.parent / "scope-correction.json"
    if correction.exists():
        evidence = json.loads(correction.read_text())
        if evidence["raw_sha256"] != digest(path):
            raise RuntimeError("CORRECTION_SOURCE_CHANGED")
        value = evidence["corrected_result"]
    return value


def recheck_scope(root, case_id, variant):
    """只读原始轨迹重建 Scope；新增修正报告，不修改或重跑原始任务。"""
    manifest = json.loads((root / "manifest.json").read_text())
    case = next(c for c in manifest["cases"] if c["id"] == case_id)
    stage = root / case_id / variant
    raw = stage / "result.json"
    result = json.loads(raw.read_text())
    transcript = rows(Path(result["session"]) / "transcript.jsonl")
    result["tool_arguments"] = {r["payload"]["call_id"]: dict(
        name=r["payload"]["tool_name"], arguments=r["payload"]["arguments"])
        for r in transcript if r["type"] == "tool_execution_started"}
    precheck = json.loads((stage / "precheck.json").read_text())
    write = json.loads((root / case_id / "WRITE/result.json").read_text())
    result.update(query_oracles(case, variant, result, write["write_oracle"], precheck["memory_before"],
                               result["memory_snapshot"], precheck["fresh"], precheck["permission_probes"]))
    if precheck["memory_before"] != write["memory_snapshot"]:
        result["scope_isolation_oracle"] = "FAIL"
    dump(stage / "scope-correction.json", dict(revision="scope-oracle-v2", raw_sha256=digest(raw),
         raw_result=str(raw), original_scope=json.loads(raw.read_text())["scope_isolation_oracle"],
         reason="成功的空 glob 未返回任何事实，不应仅因工具调用而判定泄漏；核查参数、结果、Memory 快照及请求凭证",
         corrected_result=result))
    print(json.dumps(dict(scope=result["scope_isolation_oracle"], correction=str(stage / "scope-correction.json")), ensure_ascii=False))


def summarize_run(root):
    manifest = json.loads((root / "manifest.json").read_text())
    cases = []
    pairs = []
    for case in manifest["cases"]:
        stages = {v: load_result(p) for v in ("WRITE", "ON", "OFF")
                  if (p := root / case["id"] / v / "result.json").exists()}
        valid = stages.get("WRITE", {}).get("write_oracle", {}).get("status") == "PASS" and pair_valid({k: v for k, v in stages.items() if k != "WRITE"})
        cases.append(dict(case_id=case["id"], order=case["order"], valid_pair=valid, stages=stages))
        if valid:
            pairs.append(stages)
    def metrics(population, denominator):
        if not denominator:
            return None
        on = sum(p.get("ON", {}).get("answer_correct", False) and p["ON"].get("injection_success", False) for p in population)
        off = sum(p.get("OFF", {}).get("answer_correct", False) for p in population)
        return dict(denominator=denominator, on_grounded_recall_accuracy=on / denominator,
                    off_answer_accuracy=off / denominator, memory_lift_pp=100 * (on - off) / denominator,
                    retrieval_hit_rate=sum(p.get("ON", {}).get("retrieval_hit") is True for p in population) / denominator,
                    injection_success_rate=sum(p.get("ON", {}).get("injection_success") is True for p in population) / denominator)
    token_pairs = [p for p in pairs if all(p[v]["usage_completeness"] == "COMPLETE" for v in ("ON", "OFF"))]
    unsupported = {}
    for variant in ("ON", "OFF"):
        observed = [c["stages"][variant]["unsupported_answer"] for c in cases
                    if variant in c["stages"] and c["stages"][variant].get("unsupported_answer") is not None]
        unsupported[variant] = dict(observed=len(observed), unsupported=sum(observed),
                                   rate=sum(observed) / len(observed) if observed else None,
                                   planned=6, unavailable=6-len(observed))
    report = dict(cases=cases, registered_cases=6, executed_writes=sum("WRITE" in c["stages"] for c in cases),
                  complete_pairs=sum(set(c["stages"]) == {"WRITE", "ON", "OFF"} for c in cases), valid_pairs=len(pairs),
                  all_registered=metrics([c["stages"] for c in cases], 6), valid_execution=metrics(pairs, len(pairs)),
                  all_registered_definition="未执行或无有效证据记为未证明成功；Smoke 中不是完整成功率估计",
                  unsupported_answer_rate=unsupported,
                  unsupported_definition="非 null 断言没有注入依据或与 Gold 不符；OFF 即使碰巧答对也无已知依据",
                  token_comparable_pairs=len(token_pairs),
                  provider_token_overhead=[dict(case=p["ON"]["case_id"],
                     input=p["ON"]["provider_tokens"]["input"]-p["OFF"]["provider_tokens"]["input"],
                     output=p["ON"]["provider_tokens"]["output"]-p["OFF"]["provider_tokens"]["output"]) for p in token_pairs],
                  agent_latency_difference_ms=[dict(case=p["ON"]["case_id"],
                    on_minus_off=p["ON"]["total_agent_latency_ms"]-p["OFF"]["total_agent_latency_ms"]) for p in pairs],
                  limitations=manifest["limitations"],
                  status="SMOKE_ONLY" if len(pairs) < 6 else "M1_COMPLETE_NOT_GENERAL_BENCHMARK")
    report["smoke_gate"] = "PASS" if all(c["valid_pair"] and all(
        c["stages"][v]["retrieval_oracle"] == c["stages"][v]["injection_oracle"] == "PASS"
        for v in ("ON", "OFF")) for c in cases[:2]) else "NOT_PASS"
    path = root / ("report-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    dump(path, report)
    print(json.dumps(dict(report=str(path), valid_pairs=len(pairs), status=report["status"]), ensure_ascii=False))
    return report


def run(root, remaining=False):
    manifest = json.loads((root / "manifest.json").read_text())
    if source_fingerprint() != manifest["runtime_sha256"]:
        raise RuntimeError("SOURCE_SNAPSHOT_MISMATCH")
    if remaining:
        prior = summarize_run(root)
        if prior["smoke_gate"] != "PASS":
            raise RuntimeError("SMOKE_GATE_NOT_PASSED")
    selected = manifest["cases"][2:] if remaining else manifest["cases"][:2]
    try:
        for case in selected:
            existing = {v: load_result(p) for v in ("WRITE", "ON", "OFF")
                        if (p := root / case["id"] / v / "result.json").exists()}
            if existing:
                if (existing.get("WRITE", {}).get("write_oracle", {}).get("status") == "PASS" and
                    pair_valid({k: v for k, v in existing.items() if k != "WRITE"}) and
                    all(existing[v]["retrieval_oracle"] == existing[v]["injection_oracle"] == "PASS" for v in ("ON", "OFF"))):
                    print(json.dumps(dict(case=case["id"], action="PRESERVE_COMPLETED_PAIR_NO_MODEL_CALL")), flush=True)
                    continue
                raise RuntimeError("EXISTING_INCOMPLETE_OR_FAILED_SAMPLE_NO_AUTOMATIC_RETRY")
            write = execute_stage(root, manifest, case, "WRITE")
            if write["write_oracle"]["status"] != "PASS" or write["exception"] or not write["source_snapshot_matches"]:
                raise RuntimeError("WRITE_OR_PROVIDER_FAILURE_STOP")
            pair = {}
            for variant in case["order"]:
                result = execute_stage(root, manifest, case, variant, write)
                pair[variant] = result
                if result["exception"] or result["terminal_status"] != "completed" or result["scope_isolation_oracle"] != "PASS" or not result["source_snapshot_matches"] or not result["provider_observation_complete"]:
                    raise RuntimeError("PROVIDER_OR_SCOPE_FAILURE_STOP")
                if result["retrieval_oracle"] != "PASS" or result["injection_oracle"] != "PASS":
                    raise RuntimeError("RETRIEVAL_OR_INJECTION_GATE_FAILED")
            if pair["ON"]["actual_provider_models"] != pair["OFF"]["actual_provider_models"] or pair["ON"]["actual_provider_models"] != write["actual_provider_models"]:
                raise RuntimeError("PROVIDER_CONFIGURATION_MISMATCH")
    finally:
        summarize_run(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "smoke", "remaining", "report", "recheck-scope"))
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--case", choices=tuple(c["id"] for c in CASES))
    parser.add_argument("--variant", choices=("ON", "OFF"))
    args = parser.parse_args()
    root = args.output_dir.resolve()
    if args.command == "prepare":
        prepare(root)
    elif args.command == "report":
        summarize_run(root)
    elif args.command == "recheck-scope":
        if not args.case or not args.variant:
            parser.error("Scope 重审需要 --case 和 --variant")
        recheck_scope(root, args.case, args.variant)
    else:
        if not args.execute:
            parser.error("真实模型执行必须明确 --execute")
        run(root, remaining=args.command == "remaining")


if __name__ == "__main__":
    main()
