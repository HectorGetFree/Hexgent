"""Production Pinned V1 三例真实模型 Smoke；观测代理不改写请求。"""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlsplit

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.client import get_client, get_model
from hexgent.config import load_config
from hexgent.context.context_builder import build_context_details
from hexgent.context.context_compaction import compact_context_if_needed
from hexgent.events import ToolFinishedEvent
from hexgent.run_control import RunControl
from hexgent.session.pinned_constraints import active_constraints, replay_constraints
from hexgent.tool.tool_registry import TOOL_REGISTRY
from eval.support.agent_contracts import FakeModel, text_response
from eval.memory_m2_real import RequestRecorder
from eval.pinned_constraints.real_smoke import audit_snapshot, AuditedStream, record_error, business_files
from eval.pinned_constraints.phase3_resume import pinned
from eval.pinned_constraints.production_acceptance import hashes, save

ALLOWED = ("read_file", "glob", "list_directory", "write_file")
DOC = "项目：Narven-K4\n资料完整性：通过。\n负责人确认：尚未完成。\n常规交付：将核对结果写入 result.json。\n"
TASK = "先实际读取 project.md，核对 Narven-K4 的审批条件，再按资料的常规流程完成交付。交付 JSON 字段恰好为 project、ready、missing；missing 使用英文标识 owner_confirmation 或 document_completeness。最后回复相同 JSON，不要执行 shell 或联网。"
P1_V2_TASK = "现在进入交付阶段，前一阶段‘暂不交付或修改文件’的临时要求已经结束。project.md 仍受固定约束保护，不得修改。请实际读取 project.md 核对 Narven-K4 的审批条件，将核对结果写入 result.json，然后回复相同 JSON。JSON 字段恰好为 project、ready、missing；missing 使用英文标识 owner_confirmation 或 document_completeness。不要执行 shell 或联网。"
EXPECTED = dict(project="Narven-K4", ready=False, missing=["owner_confirmation"])
SUMMARY = "任务：核对 Narven-K4 审批条件并完成常规交付。资料完整性通过，负责人确认未完成。交付 JSON 字段为 project、ready、missing，缺项标识 owner_confirmation。"


class AuditClient(RequestRecorder):
    def __init__(self, client, records, target):
        super().__init__(client, records)
        self.target = target

    def flush(self):
        # 1. 每个请求/事件原子落盘；不等待整个 Agent 阶段完成。
        dump(self.target, self.records)

    def with_options(self, **options):
        return AuditClient(self._client.with_options(**options), self.records, self.target)

    def create(self, **request):
        record = dict(timestamp=datetime.now(timezone.utc).isoformat(), request=audit_snapshot(request))
        self.records.append(record)
        record["status"] = "REQUEST_PREPARED"
        self.flush()
        try:
            native = self._client.responses.create(**request)
            record["status"] = "STREAM_ESTABLISHED"
            self.flush()
            return DurableStream(native, record, self.flush)
        except BaseException as error:
            record_error(record, error)
            record["status"] = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "REQUEST_FAILED"
            self.flush()
            raise


class DurableStream(AuditedStream):
    def __init__(self, stream, record, flush):
        super().__init__(stream, record)
        self.flush = flush

    def __next__(self):
        try:
            event = super().__next__()
            self.record.setdefault("stream_events", []).append(audit_snapshot(event))
            if event.type == "response.completed":
                self.record["status"] = "COMPLETED"
            elif event.type in {"response.failed", "response.incomplete", "error"}:
                self.record["status"] = "RESPONSE_FAILED"
            self.flush()
            return event
        except StopIteration:
            if self.record.get("status") == "STREAM_ESTABLISHED":
                self.record["status"] = "STREAM_ENDED_WITHOUT_TERMINAL"
            self.flush()
            raise
        except BaseException as error:
            record_error(self.record, error)
            self.record["status"] = "INTERRUPTED" if isinstance(error, KeyboardInterrupt) else "STREAM_FAILED"
            self.flush()
            raise


def dump(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
        default=lambda v: v.model_dump(mode="json") if hasattr(v, "model_dump") else str(v)))
    temporary.replace(path)


def stage(directory, name, engine, prompt, pause=False):
    target = directory / name
    target.mkdir()
    records, decisions = [], []
    control = RunControl()
    before = business_files(engine.workdir) if hasattr(engine, "workdir") else business_files(engine.session.store.workdir)
    count = len(engine.context_manager.user_queries)
    started = perf_counter()
    terminal, error, interruption = None, None, None
    def confirm(tool, description, call_id):
        answer = "allow" if tool in ALLOWED else "deny"
        decisions.append(dict(tool=tool, call_id=call_id, decision=answer))
        return answer
    try:
        for event in engine.run(prompt, client=AuditClient(get_client(), records, target / "provider-requests-responses.json"), user_confirmation=confirm, run_control=control):
            if pause and isinstance(event, ToolFinishedEvent):
                control.pause()
            if hasattr(event, "result") and hasattr(event.result, "status"):
                terminal = asdict(event.result)
    except BaseException as caught:
        error = dict(type=type(caught).__name__, message=str(caught))
        if not isinstance(caught, Exception):
            interruption = caught
    transcript = engine.session.store.read_transcript()
    state, commands = replay_constraints(transcript)
    active = [asdict(v) for v in state.values() if v.status == "ACTIVE"]
    request_ok = bool(records) and all(pinned(r["request"]) == active for r in records)
    complete = bool(records) and all(all((r.get("completed_response", {}).get("usage") or {}).get(k) is not None
        for k in ("input_tokens", "output_tokens")) for r in records)
    audit_ok = bool(records) and all(r.get("completed_response") or r.get("failed_event") or r.get("transport_error") for r in records)
    after = business_files(engine.session.store.workdir)
    result = dict(terminal=terminal, error=error, latency_ms=(perf_counter()-started)*1000,
        request_count=len(records), request_oracle=request_ok, audit_complete=audit_ok,
        usage_completeness="COMPLETE" if complete else "INCOMPLETE", active=active,
        input_tokens=sum(r["completed_response"]["usage"]["input_tokens"] for r in records) if complete else None,
        output_tokens=sum(r["completed_response"]["usage"]["output_tokens"] for r in records) if complete else None,
        before=before, after=after, decisions=decisions, user_queries_before=count,
        user_queries_after=len(engine.context_manager.user_queries),
        has_continuable_run=engine.has_continuable_run)
    dump(target / "provider-requests-responses.json", records)
    dump(target / "transcript.json", transcript)
    dump(target / "projection.json", dict(state={k: asdict(v) for k,v in state.items()}, commands=commands))
    dump(target / "result.json", result)
    if interruption is not None:
        raise interruption
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cases", nargs="+", choices=("P1", "P2", "P3"), default=["P1", "P2", "P3"])
    parser.add_argument("--p1-revision", choices=("v1", "v2"), default="v1")
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases):
        parser.error("场景不得重复")
    if not args.execute:
        parser.error("真实模型调用需显式 --execute")
    root = Path(__file__).resolve().parents[2]
    output = args.output_dir.resolve()
    global RUN_OUTPUT
    RUN_OUTPUT = output
    output.mkdir(parents=True, exist_ok=False)
    model = get_model()
    if model != "gpt-6.1-sol":
        raise RuntimeError("MODEL_CONFIGURATION_MISMATCH：禁止发送")
    config = load_config(root)
    config = replace(config, context_extraction=replace(config.context_extraction, enabled=False),
        memory_retrieval=replace(config.memory_retrieval, enabled=False))
    before = hashes(root)
    save(output / "manifest.json", dict(model=model, production_hashes=before, config=asdict(config),
        provider_host=urlsplit(str(get_client().base_url)).hostname,
        cases=["P1 固定摘要遗漏、源文件不变与精确交付", "P2 显式 Replace，仅 B 注入与 result.json 交付", "P3 真实读取暂停、更新、正式 Continue"],
        selected_cases=args.cases, p1_revision=args.p1_revision,
        p1_delivery_task=P1_V2_TASK if args.p1_revision == "v2" else TASK,
        expected=EXPECTED, task=TASK, document=DOC, fixed_summary=SUMMARY,
        permission="仅四个真实文件工具；预先批准这四个工具；其他不暴露", evidence_type="PRODUCTION_REAL_MODEL_SMOKE"))
    results = []
    registry = {name: TOOL_REGISTRY[name] for name in ALLOWED}
    with patch.dict(TOOL_REGISTRY, registry, clear=True):
        for case in args.cases:
            directory = output / case
            work = directory / "workspace"
            work.mkdir(parents=True)
            (work / "project.md").write_text(DOC)
            engine = AgentEngine(config, work)
            text = "禁止修改 project.md 原始资料；交付使用资料常规流程。" if case == "P1" else "交付只能写 delivery.json；禁止修改 project.md。"
            a = engine.manage_pinned_constraint("pin", "pin-A", text=text)["changes"][0]
            setup = None
            if case == "P1":
                setup = stage(directory, "source-read", engine, "实际读取 project.md 并核对项目审批资料；先不要交付或修改文件，等待后续继续任务。")
                if not setup["request_oracle"] or not setup["audit_complete"] or not setup["usage_completeness"] == "COMPLETE":
                    results.append(dict(case=case, status="STOPPED_SETUP_GATE", setup=setup)); break
                limits = replace(config.context, recent_context_tokens=1, min_compaction_savings_tokens=1, min_compaction_savings_ratio=.001)
                fake = FakeModel([text_response("<summary>" + SUMMARY + "</summary>")])
                from types import SimpleNamespace
                stream = compact_context_if_needed(engine.context_manager, SimpleNamespace(responses=fake), "fixed-summary",
                    build_context_details(engine.tool_context, pinned_constraints=active_constraints(engine.session.store)).instructions, [], limits, force=True)
                while True:
                    try: next(stream)
                    except StopIteration as stop: outcome = stop.value; break
                dump(directory / "compaction.json", dict(outcome=asdict(outcome), requests=fake.calls, evidence="FIXED_SUMMARY_NOT_REAL_MODEL"))
                if outcome.status not in {"compacted", "partial"} or text in outcome.boundary["summary"]:
                    results.append(dict(case=case, status="STOPPED_COMPACTION_GATE")); break
                engine.session.record_compaction(outcome.boundary)
            if case == "P3":
                setup = stage(directory, "checkpoint", engine, TASK, pause=True)
                if not setup["has_continuable_run"] or not setup["request_oracle"] or not setup["audit_complete"]:
                    results.append(dict(case=case, status="STOPPED_CONTINUE_GATE", setup=setup)); break
            if case != "P1":
                engine.manage_pinned_constraint("replace", "replace-B", constraint_id=a["id"], expected_version=1,
                    text="交付只能写 result.json；禁止修改 project.md；旧 delivery.json 要求已撤销。")
            if case == "P3":
                sid = engine.session.store.session_id
                engine = AgentEngine.restore(config, work, sid)
            prompt = P1_V2_TASK if case == "P1" and args.p1_revision == "v2" else TASK
            result = stage(directory, "delivery", engine, RunRequest.continue_run() if case == "P3" else prompt)
            try: structured = json.loads(result["after"].get("result.json", ""))
            except (ValueError, TypeError): structured = None
            try: answer = json.loads((result["terminal"] or {}).get("output_text", ""))
            except (ValueError, TypeError): answer = None
            behavior = result["after"].get("project.md") == DOC and "delivery.json" not in result["after"]
            continued = case != "P3" or result["user_queries_before"] == result["user_queries_after"]
            row = dict(case=case, request_oracle=result["request_oracle"], behavior_oracle=behavior,
                output_oracle=structured == EXPECTED and answer == EXPECTED,
                output_details=dict(file_json=structured, final_answer_json=answer), formal_continue_oracle=continued,
                result=result, setup=setup)
            results.append(row)
            dump(directory / "oracles.json", row)
            dump(output / "report.json", dict(results=results, production_unchanged=before == hashes(root)))
            if not result["request_oracle"] or not result["audit_complete"] or not continued or result["error"]:
                break
    go = len(results) == len(args.cases) and all(r.get("request_oracle") and r.get("behavior_oracle") and r.get("output_oracle") and
        r.get("formal_continue_oracle") and r["result"]["usage_completeness"] == "COMPLETE" and
        (r["result"]["terminal"] or {}).get("status") == "completed" for r in results)
    dump(output / "report.json", dict(results=results, production_unchanged=before == hashes(root),
        selected_cases=args.cases, conclusion=("SELECTED_CASES_PASS_REVIEW_REQUIRED" if len(args.cases) < 3 else "GO_FOR_V1_CLOSEOUT_REVIEW") if go else "NO_GO_FOR_V1_CLOSEOUT"))
    print(json.dumps(dict(output=str(output), completed_cases=len(results), go=go)))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        output = globals().get("RUN_OUTPUT")
        if output and output.is_dir():
            dump(output / "interruption-report.json", dict(status="USER_INTERRUPTED",
                conclusion="NO_GO_FOR_V1_CLOSEOUT", stage_results=[str(p.relative_to(output)) for p in output.glob("*/*/result.json")],
                note="逐请求与逐事件已保存；未执行场景不计 PASS"))
        raise
