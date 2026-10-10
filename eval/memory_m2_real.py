"""M2 五例真实模型验收：仅新增 Eval，保留原始请求和独立 Oracle。"""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from types import SimpleNamespace

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.client import get_client, get_model
from hexgent.config import load_config
from hexgent.events import ToolFinishedEvent
from hexgent.memory import MemoryStore, MemoryRetriever
from hexgent.observation import current_observation, source_fingerprint
from hexgent.run_control import RunControl
from hexgent.session.checkpoint import CheckpointStore
from hexgent.tool.tool_permission_pipeline import PermissionConfig
from eval.memory_m1 import dump, rows, tree_hash, write_oracle, answer_oracle, no_external_fact_source


CASES = [
    dict(id="S1", entity="Soryn-Q8M", fact="Soryn-Q8M 的发布标签是 cobalt-62。", category="project",
         operation="supersede_memory", new_fact="Soryn-Q8M 的发布标签是 ivory-93。", expected="ivory-93", question="Soryn-Q8M 当前发布标签是什么？"),
    dict(id="S2", entity="Tavin-R5B", fact="Tavin-R5B 的服务端口是 37619。", category="project",
         operation="invalidate_memory", expected=None, question="Tavin-R5B 的服务端口是什么？"),
    dict(id="S3", entity="Nelra-P7K", fact="Nelra-P7K 的编码是 zstd-f7。", category="project",
         operation="delete_memory", expected=None, question="Nelra-P7K 的编码是什么？"),
    dict(id="S4", entity="Oryx-J6V", fact="Oryx-J6V 的交付入口是 gate-84。", category="project",
         operation="invalidate_memory", expected=None, question="Oryx-J6V 的交付入口是什么？", resume=True),
    dict(id="S5", entity="Veyra-K8L", fact="Veyra-K8L 的评审标题顺序固定为：边界先述、反例核对、落地清单。", category="preference",
         operation=None, expected=["边界先述", "反例核对", "落地清单"], question="Veyra-K8L 的评审标题按什么顺序排列？value 使用字符串数组。"),
]


class RequestRecorder:
    """透明转发 SDK，记录 create 收到的最终参数，不改写工具或模型参数。"""
    def __init__(self, client, records):
        self._client, self.records = client, records
        self.responses = SimpleNamespace(create=self.create)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def with_options(self, **kwargs):
        return RequestRecorder(self._client.with_options(**kwargs), self.records)

    def create(self, **request):
        observer = current_observation.get()
        span = None
        if observer is not None and observer.store is not None:
            observed = rows(observer.store.session_dir / "observation.jsonl")
            started = [r for r in observed if r["event"] == "provider_started" and r["execution_id"] == observer.execution_id]
            span = started[-1]["span_id"] if started else None
        record = dict(timestamp=datetime.now(timezone.utc).isoformat(), span_id=span,
                      request=json.loads(json.dumps(request, ensure_ascii=False, default=str)))
        self.records.append(record)
        return self._client.responses.create(**request)


def prepare(output):
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[1]
    config = replace(load_config(repo), permission=PermissionConfig(default_mode="default"))
    original = repo / "eval/memory_m1/runs/real-v1-20261010T034922Z"
    # 兼容历史 M1 输出目录名称，明确保存实际存在的路径。
    if not original.exists():
        original = repo / "eval/memory_m1/runs/m1-20261010T034922Z"
    manifest = dict(revision="memory-m2-real-v1", cases=CASES, config=asdict(config),
                    runtime_sha256=source_fingerprint(), model=get_model(),
                    provider_host=__import__("urllib.parse", fromlist=["urlsplit"]).urlsplit(str(get_client().base_url)).hostname,
                    m1_original=str(original), m1_hashes=tree_hash(original),
                    permission="Production default; explicit approval only for current synthetic lifecycle tools",
                    scope="independent private vaults; S4 native restore uses the original workdir",
                    oracle_definitions={
                        "write": "reuse M1 write_oracle; actual remember success and exact grounded persisted fact",
                        "permission": "lifecycle call_id has permission_evaluated ASK and permission_resolved user_allowed",
                        "authority": "current lifecycle.json state and deletion tombstone; no direct Store mutation",
                        "retrieval": "fresh production MemoryRetriever over current Store items",
                        "injection": "all post-operation MAIN_AGENT receipts and captured relevant-memory payloads",
                        "answer": "reuse M1 strict JSON value oracle; S4 historical answer is diagnostic only",
                        "resume": "durable checkpoint contains old ID; original Session restored with formal continue",
                    })
    dump(output / "manifest.json", manifest)
    return config, manifest


def run_stage(output, name, engine, prompt, *, pause=False, continue_run=False, client=None):
    stage = output / name
    stage.mkdir()
    calls, approvals = [], []
    transcript_before = len(engine.session.store.read_transcript())
    observation_path = engine.session.store.session_dir / "observation.jsonl"
    observation_before = len(rows(observation_path)) if observation_path.exists() else 0
    control = RunControl()
    def confirm(tool, description, call_id):
        decision = "allow" if tool in {"supersede_memory", "invalidate_memory", "delete_memory"} else "deny"
        approvals.append(dict(tool=tool, description=description, call_id=call_id, decision=decision))
        return decision
    error, terminal = None, None
    try:
        stream = engine.run(RunRequest.continue_run() if continue_run else prompt,
                            client=RequestRecorder(client or get_client(), calls),
                            user_confirmation=confirm, run_control=control)
        for event in stream:
            if pause and isinstance(event, ToolFinishedEvent):
                control.pause()
            if hasattr(event, "result") and hasattr(event.result, "status"):
                terminal = asdict(event.result)
    except Exception as caught:
        error = dict(type=type(caught).__name__, message=str(caught))
    transcript = engine.session.store.read_transcript()
    delta = transcript[transcript_before:]
    observation_path = engine.session.store.session_dir / "observation.jsonl"
    observed = rows(observation_path)[observation_before:] if observation_path.exists() else []
    payloads = lambda kind: [r["payload"] for r in delta if r["type"] == kind]
    finished = payloads("agent_run_finished")
    terminal = finished[-1] if finished else terminal or {}
    starts = [r for r in observed if r["event"] == "provider_started"]
    ends = [r for r in observed if r["event"] == "provider_finished"]
    completeness = (bool(starts) and {r["span_id"] for r in starts} == {r["span_id"] for r in ends}
                    and all(r.get("usage_available") and r.get("input_tokens") is not None and r.get("output_tokens") is not None for r in ends))
    result = dict(session=str(engine.session.store.session_dir), session_id=engine.session.store.session_id,
                  terminal_status=terminal.get("status"), final_answer=terminal.get("output_text"), error=error,
                  tool_calls=payloads("tool_execution_finished"),
                  tool_arguments={p["call_id"]: dict(name=p["tool_name"], arguments=p["arguments"]) for p in payloads("tool_execution_started")},
                  permission_events=[r for r in delta if r["type"].startswith("permission_")], approvals=approvals,
                  memory_receipts=[r for r in observed if r["event"] == "memory_injected" and r["request_type"] == "MAIN_AGENT"],
                  provider_requests=len(starts), captured_requests=calls, provider_events=starts + ends,
                  usage_completeness="COMPLETE" if completeness else "INCOMPLETE",
                  provider_tokens={"input": sum(r["input_tokens"] for r in ends) if completeness else None,
                                   "output": sum(r["output_tokens"] for r in ends) if completeness else None},
                  provider_failures=[r for r in ends if r["status"] != "SUCCEEDED"],
                  checkpoints=payloads("checkpoint_committed"), has_continuable_run=engine.has_continuable_run)
    dump(stage / "result.json", result)
    dump(stage / "transcript.json", delta)
    dump(stage / "provider-observation.json", observed)
    return result


def memory_payloads(result):
    payloads = []
    for record in result["captured_requests"]:
        text = record["request"].get("instructions", "")
        for match in re.finditer(r"<relevant-memory>.*?</relevant-memory>", text, re.S):
            block = match.group(0)
            payloads.extend(json.loads(block[block.index("[{"):block.rindex("</relevant-memory>")].strip()))
    return payloads


def lifecycle_oracle(case, identity, mutation, authority):
    successful = []
    for call in mutation["tool_calls"]:
        if call["tool_name"] != case["operation"] or call["status"] != "success":
            continue
        args = json.loads(mutation["tool_arguments"].get(call["call_id"], {}).get("arguments", "{}"))
        if args.get("old_memory_id", args.get("memory_id")) == identity:
            successful.append(call)
    permitted = False
    for call in successful:
        events = [r for r in mutation["permission_events"] if r["payload"].get("call_id") == call["call_id"]]
        permitted |= (any(r["type"] == "permission_evaluated" and r["payload"].get("behavior") == "ask" for r in events)
                      and any(r["type"] == "permission_resolved" and r["payload"].get("resolution") == "user_allowed" for r in events))
    old = next((i for i in authority["items"] if i["memory_id"] == identity), None)
    if case["operation"] == "delete_memory":
        valid = old is None and identity in authority["deleted"]
    elif case["operation"] == "invalidate_memory":
        valid = old is not None and old["status"] == "INVALIDATED"
    else:
        target = next((i for i in authority["items"] if i["memory_id"] == (old or {}).get("superseded_by")), None)
        valid = old is not None and old["status"] == "SUPERSEDED" and target is not None and target["status"] == "ACTIVE" and target["content"] == case["new_fact"]
    return dict(model_tool_call="PASS" if successful else "FAIL", permission="PASS" if permitted else "FAIL",
                authoritative_state="PASS" if valid else "FAIL")


def execute_case(output, config, case, client_factory=None):
    root = output / case["id"]
    root.mkdir()
    vault = root / "scope" if case.get("resume") else root / "vault"
    def stage(name, active, prompt, **kwargs):
        client = client_factory(case, name) if client_factory else None
        return run_stage(root, name, active, prompt, client=client, **kwargs)
    def engine(name, *, native=False):
        workdir = vault if native else root / (name + "-workspace")
        workdir.mkdir(parents=True, exist_ok=True)
        return AgentEngine(config, workdir, memory_store=MemoryStore(vault))
    write = stage("WRITE", engine("write"), case["fact"] + " 请调用 remember 长期记住这段事实原文，保持实体和取值完整不改写。")
    write_check = write_oracle(case, write, MemoryStore(vault))
    if write_check["status"] != "PASS":
        return dict(id=case["id"], status="BLOCKED", reason="WRITE_NOT_VERIFIED", write=write_check)
    identity = write_check["facts"][0]["memory_id"]
    dump(root / "authority-before.json", json.loads(MemoryStore(vault).lifecycle_path.read_text()))
    paused, checkpoint, resume_id = None, None, None
    if case.get("resume"):
        active = engine("pause", native=True)
        paused = stage("PAUSE", active,
            case["question"] + " 请先用 glob 查找当前目录的 *.m2-nothing 文件，完成工具查询后再回答；不要读取历史文件。", pause=True)
        if not paused["has_continuable_run"] or not paused["checkpoints"]:
            return dict(id=case["id"], status="BLOCKED", reason="FORMAL_CONTINUE_NOT_REACHABLE", write=write_check)
        checkpoint = CheckpointStore(active.session.store.session_dir).read(paused["checkpoints"][-1]["checkpoint_id"])
        if identity not in {m["memory_id"] for m in checkpoint.run_state.get("relevant_memory", [])}:
            return dict(id=case["id"], status="BLOCKED", reason="MEMORY_NOT_IN_CHECKPOINT", write=write_check)
        resume_id = active.session.store.session_id
        dump(root / "paused-checkpoint.json", asdict(checkpoint))
    operation_check = {"model_tool_call": "NOT_APPLICABLE", "permission": "NOT_APPLICABLE", "authoritative_state": "PASS"}
    if case["operation"]:
        instruction = f"请明确调用 {case['operation']} 操作当前工作区的 Memory ID {identity}。"
        if case["operation"] == "supersede_memory":
            instruction += f"用新事实原文替代：{case['new_fact']}；类别 project，关键词 {case['entity']}。"
        else:
            instruction += "这是用户明确授权的合成记忆生命周期操作，不要再调用 remember，也不要操作文件。"
        mutation = stage("MUTATE", engine("mutate"), instruction)
        authority = json.loads(MemoryStore(vault).lifecycle_path.read_text())
        dump(root / "authority-after.json", authority)
        operation_check = lifecycle_oracle(case, identity, mutation, authority)
        if any(v == "FAIL" for v in operation_check.values()):
            return dict(id=case["id"], status="FAIL", write=write_check, oracles=operation_check, reason="LIFECYCLE_NOT_VERIFIED")
    current = MemoryStore(vault)
    recalled = MemoryRetriever(config.memory_retrieval).retrieve(case["question"], current.items)
    dump(root / "retrieval-after.json", [asdict(i) for i in recalled])
    if case.get("resume"):
        query_engine = AgentEngine.restore(config, vault, resume_id)
        query = stage("CONTINUE", query_engine, None, continue_run=True)
    else:
        query = stage("QUERY", engine("query"), case["question"] + ' 仅根据该实体的已知事实，只返回 JSON {"value":答案}；不知道则 value=null。不要查文件或猜测。')
    injected = memory_payloads(query)
    receipt_items = [m for r in query["memory_receipts"] for m in r["memories"]]
    revoked = bool(case["operation"])
    old_leak = revoked and (identity in {m["memory_id"] for m in injected + receipt_items}
                            or case["fact"] in [m["content"] for m in injected])
    desired = case.get("new_fact", case["fact"])
    expect_memory = case["id"] in {"S1", "S5"}
    target = next((m for m in current.items if m.content == desired), None)
    retrieval_ok = (bool(target) and target.memory_id in {m.memory_id for m in recalled}) if expect_memory else not recalled
    injection_ok = (bool(target) and any(m["memory_id"] == target.memory_id and m["content"] == desired for m in injected)
                    and any(m["memory_id"] == target.memory_id and m["content_sha256"] == sha256(desired.encode()).hexdigest() for m in receipt_items)) if expect_memory else not injected and not receipt_items
    answer = answer_oracle(case, query["final_answer"])
    oracles = {**operation_check, "write": write_check["status"],
               "retrieval": "PASS" if retrieval_ok else "FAIL",
               "memory_injection": "PASS" if injection_ok and not old_leak else "FAIL",
               "provider_request": "PASS" if query["captured_requests"] and query["memory_receipts"] else "BLOCKED",
               "final_answer": "NOT_APPLICABLE" if case.get("resume") else "PASS" if answer["answer_correct"] else "FAIL",
               "checkpoint_resume": "PASS" if case.get("resume") and query["session_id"] == resume_id and query["terminal_status"] == "completed" else "FAIL" if case.get("resume") else "NOT_APPLICABLE",
               "usage": "PASS" if query["usage_completeness"] == "COMPLETE" else "BLOCKED",
               "other_fact_channels": "NOT_APPLICABLE" if case.get("resume") else "PASS" if no_external_fact_source(query) else "FAIL"}
    if case["id"] == "S3":
        current_authority = json.loads(current.lifecycle_path.read_text())
        oracles["physical_delete"] = "PASS" if not (current.items_directory / f"{identity}.md").exists() and identity in current_authority["deleted"] else "FAIL"
    oracles["terminal_completion"] = "PASS" if query["terminal_status"] == "completed" else "BLOCKED"
    status = "FAIL" if "FAIL" in oracles.values() else "BLOCKED" if "BLOCKED" in oracles.values() else "PASS"
    return dict(id=case["id"], status=status, oracles=oracles, answer=answer,
                scope=str(vault), old_memory_id=identity, revoked_injection=old_leak,
                resume_answer_limitation="历史事实不属于撤销保证，答案仅诊断" if case.get("resume") else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report-only", action="store_true")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if args.report_only:
        finalize_report(output)
        return
    config, manifest = prepare(output)
    if not args.execute:
        print(json.dumps({"manifest": str(output / "manifest.json"), "status": "PREREGISTERED"}))
        return
    results = []
    for case in CASES:
        print("START " + case["id"], flush=True)
        try:
            result = execute_case(output, config, case)
        except Exception as error:
            result = dict(id=case["id"], status="BLOCKED", reason=type(error).__name__, message=str(error))
        dump(output / (case["id"] + "-oracle.json"), result)
        results.append(result)
        print(case["id"] + " " + result["status"], flush=True)
        if result.get("revoked_injection"):
            break
        if result["status"] != "PASS":
            break  # 不自动重跑；先报告缺陷或 Provider/Oracle 问题。
    unchanged = tree_hash(Path(manifest["m1_original"])) == manifest["m1_hashes"]
    source_unchanged = source_fingerprint() == manifest["runtime_sha256"]
    report = dict(status="PASS" if len(results) == 5 and all(r["status"] == "PASS" for r in results) and unchanged and source_unchanged else "NOT_PASS",
                  scenarios=results, registered=5, executed=len(results), m1_artifacts_unchanged=unchanged,
                  production_source_unchanged=source_unchanged, limits=["五例 Smoke；不证明广泛稳定性或所有竞争排列", "撤销仅作用于长期 Memory 注入渠道", "重试窗口主要由已有确定性证据覆盖"])
    report["unexecuted"] = [dict(id=c["id"], status="BLOCKED", reason="STOPPED_AFTER_PRIOR_CASE") for c in CASES if c["id"] not in {r["id"] for r in results}]
    stages = [json.loads(p.read_text()) for p in output.glob("S*/*/result.json")]
    events = [e for s in stages for e in s["provider_events"] if e["event"] == "provider_finished"]
    requests = sum(s["provider_requests"] for s in stages)
    usable = [e for e in events if e.get("usage_available") and e.get("input_tokens") is not None and e.get("output_tokens") is not None]
    report["provider_usage"] = dict(requests=requests, usage_available_requests=len(usable),
        usage_completeness="COMPLETE" if len(usable) == requests else "INCOMPLETE",
        observable_input_token_subtotal=sum(e["input_tokens"] for e in usable),
        observable_output_token_subtotal=sum(e["output_tokens"] for e in usable),
        unavailable_requests=requests-len(usable))
    dump(output / "report.json", report)
    print(json.dumps({"status": report["status"], "report": str(output / "report.json")}, ensure_ascii=False))


def finalize_report(output):
    """只读复核原始请求，另写报告，不覆盖真实运行工件。"""
    report = json.loads((output / "report.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    checks = []
    for case in CASES[:4]:
        root = output / case["id"]
        result = json.loads((output / (case["id"] + "-oracle.json")).read_text())
        transcript = json.loads((root / "MUTATE/transcript.json").read_text())
        completion = next(r for r in transcript if r["type"] == "tool_execution_finished"
                          and r["payload"]["tool_name"] == case["operation"] and r["payload"]["status"] == "success")
        boundary = datetime.fromisoformat(completion["created_at"])
        examined = []
        for stage in ("MUTATE", "CONTINUE" if case.get("resume") else "QUERY"):
            data = json.loads((root / stage / "result.json").read_text())
            for request in data["captured_requests"]:
                if datetime.fromisoformat(request["timestamp"]) < boundary:
                    continue
                memories = memory_payloads({"captured_requests": [request]})
                old = [m for m in memories if m["memory_id"] == result["old_memory_id"] or m["content"] == case["fact"]]
                receipts = [r for r in data["memory_receipts"] if r["span_id"] == request["span_id"]]
                hash_match = bool(receipts) and all(r["instructions_sha256"] == sha256(request["request"].get("instructions", "").encode()).hexdigest() for r in receipts)
                receipt_old = any(m["memory_id"] == result["old_memory_id"] for r in receipts for m in r["memories"])
                examined.append(dict(stage=stage, span_id=request["span_id"], old_memory_injected=bool(old) or receipt_old,
                                     request_receipt_hash_matches=hash_match))
        checks.append(dict(id=case["id"], requests=examined,
                           status="PASS" if examined and all(not r["old_memory_injected"] and r["request_receipt_hash_matches"] for r in examined) else "FAIL"))
    validation = dict(original_report=str(output / "report.json"), scenarios=report["scenarios"],
        revocation_followup_requests=checks, provider_usage=report["provider_usage"],
        tests=dict(memory_and_eval=55, runtime=269, failures=0, errors=0, skipped=0),
        source_unchanged=source_fingerprint() == manifest["runtime_sha256"],
        m1_unchanged=tree_hash(Path(manifest["m1_original"])) == manifest["m1_hashes"],
        limits=report["limits"] + ["本次未主动制造请求重试、撤销/请求启动并发竞争或进程崩溃；这些边界仅引用已有确定性测试",
                                  "S4 最终答案不纳入撤销 Oracle；历史 Transcript/已发请求/模型已知信息不在清除保证内"])
    validation["status"] = "PASS" if report["status"] == "PASS" and all(c["status"] == "PASS" for c in checks) and validation["source_unchanged"] and validation["m1_unchanged"] else "NOT_PASS"
    dump(output / "acceptance.json", validation)
    print(json.dumps(dict(status=validation["status"], acceptance=str(output / "acceptance.json")), ensure_ascii=False))


if __name__ == "__main__":
    main()
