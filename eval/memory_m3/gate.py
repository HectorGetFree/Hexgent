"""M3 Phase 1：真实 Runtime + 确定性模型；发现 P0 即停止，无网络调用。"""
import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from hexgent.agent_engine import AgentEngine, RunRequest
from hexgent.config import HexgentConfig
from hexgent.events import ToolFinishedEvent
from hexgent.memory import ContextRecordInput
from hexgent.memory.context_record_extraction import ContextExtractionLimits
from hexgent.observation import source_fingerprint
from hexgent.run_control import RunControl
from hexgent.session.checkpoint import CheckpointStore
from eval.memory_m1 import dump, tree_hash
from eval.support.agent_contracts import FakeModel, text_response, tool_call

OLD = "Nerul-X7 的当前端口是 48317。"
NEW = "Nerul-X7 的当前端口是 59263。"


class OfflineClient:
    """只产生预注册响应；任何 Provider SDK 未参与执行。"""
    base_url = "https://offline.invalid"
    def __init__(self, outputs):
        self.main = FakeModel(outputs)
        self.calls = []
        self.responses = SimpleNamespace(create=self.create)
    def with_options(self, **kwargs):
        return self
    def create(self, **kwargs):
        # 1. 在实际 create 边界捕获完整最终参数。
        self.calls.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=str)))
        if kwargs.get("tools") == [] and "existing_records" in str(kwargs.get("input")):
            batch = json.loads(kwargs["input"][0]["content"])
            candidates = []
            for fact in batch["facts"]:
                if fact["type"] != "context_user_query_appended" and not (fact["type"] == "tool_execution_finished" and fact["status"] == "success"):
                    continue
                value = NEW if NEW in fact["text"] and fact["type"] == "context_user_query_appended" else OLD if OLD in fact["text"] else None
                if value:
                    supersedes = [r["record_id"] for r in batch["existing_records"] if r["content"] == OLD] if value == NEW else []
                    candidates.append(dict(content=value, category="fact", source_sequences=[fact["sequence"]], supersedes=supersedes, evidence_quote=value))
            response = text_response(json.dumps(dict(records=candidates[:4]), ensure_ascii=False))
            response.usage = None  # 不把 FakeModel fixture usage 写成真实成本。
            return iter([SimpleNamespace(type="response.completed", response=response)])
        return self.main.create(**kwargs)


def config(enabled=True):
    base = HexgentConfig()
    return replace(base, context_extraction=ContextExtractionLimits(turn_interval=1),
                   context_retrieval=replace(base.context_retrieval, enabled=enabled),
                   memory_retrieval=replace(base.memory_retrieval, enabled=False))


def run(engine, request, client, target, pause=False):
    target.mkdir(parents=True, exist_ok=False)
    control = RunControl()
    before = len(engine.session.store.read_transcript())
    for event in engine.run(request, client=client, run_control=control):
        if pause and isinstance(event, ToolFinishedEvent):
            control.pause()
    transcript = engine.session.store.read_transcript()
    dump(target / "requests.json", client.calls)
    dump(target / "transcript.json", transcript[before:])
    dump(target / "records.json", [asdict(r) for r in engine.session.context_records.records])
    terminal = [r for r in transcript[before:] if r["type"] == "agent_run_finished"][-1]["payload"]
    return dict(terminal=terminal, requests=client.calls)


def primary_requests(result):
    return [r for r in result["requests"] if r.get("tools") != []]


def run_gate(output):
    output.mkdir(parents=True, exist_ok=False)
    before_source = source_fingerprint()
    repo = Path(__file__).resolve().parents[2]
    frozen = {str(p): tree_hash(p) for p in [repo / "eval/memory_m1/runs", repo / "eval/memory_m2/runs"]}
    checks = []
    # 2. Stage A 固定实际文件工具观察；未来更正授权不用作提取候选。
    original = output / "baseline/workspace"
    original.mkdir(parents=True)
    (original / "fact.txt").write_text(OLD, encoding="utf-8")
    engine = AgentEngine(config(), original)
    run(engine, "读取 fact.txt，记录 Nerul-X7 的当前端口。", OfflineClient([
        text_response(output=[tool_call("observe", "read_file", '{"path":"fact.txt"}')]), text_response("观察完成")]), output / "stage-a")
    old_id = next(r.record_id for r in engine.session.context_records.active_records if r.content == OLD)
    snapshot = tree_hash(original)
    session_id = engine.session.store.session_id
    paired = {}
    for variant in ("ON", "OFF"):
        clone = output / variant / "workspace"
        shutil.copytree(original, clone)
        same = tree_hash(clone) == snapshot
        active = AgentEngine.restore(config(variant == "ON"), clone, session_id)
        pre = dict(snapshot_same=same, records=[asdict(r) for r in active.session.context_records.records],
                   memory=tree_hash(clone / ".hexgent/memory"), settings=asdict(active.session.settings), config=asdict(active.config))
        dump(output / variant / "precheck.json", pre)
        result = run(active, "Nerul-X7 的当前端口是什么？允许正常历史和工具回读。", OfflineClient([
            text_response(output=[tool_call("reread", "read_file", '{"path":"fact.txt"}')]), text_response("48317")]), output / variant / "execution")
        paired[variant] = dict(pre=pre, result=result)
    a, b = paired["ON"], paired["OFF"]
    ac, bc = a["pre"]["config"], b["pre"]["config"]
    ac["context_retrieval"]["enabled"] = bc["context_retrieval"]["enabled"]
    on, off = primary_requests(a["result"])[0], primary_requests(b["result"])[0]
    on_block = "<relevant-session-context>" in on["instructions"] and OLD in on["instructions"]
    off_block = "<relevant-session-context>" in off["instructions"]
    strip = __import__("re").sub(r"\s*<relevant-session-context>.*?</relevant-session-context>", "", on["instructions"], flags=__import__("re").S)
    isolation = dict(snapshot_identical=a["pre"]["snapshot_same"] and b["pre"]["snapshot_same"],
        records_identical=a["pre"]["records"] == b["pre"]["records"], memory_identical=a["pre"]["memory"] == b["pre"]["memory"],
        permissions_identical=a["pre"]["settings"] == b["pre"]["settings"], config_only_switch=ac == bc,
        initial_input_identical=on["input"] == off["input"], tool_schema_identical=on["tools"] == off["tools"],
        non_record_instructions_identical=strip.strip() == off["instructions"].strip(),
        on_injects=on_block, off_no_record=not off_block,
        both_reread=all(any(isinstance(t, dict) and t.get("type") == "function_call_output" and t.get("call_id") == "reread" for t in primary_requests(p["result"])[1]["input"]) for p in (a,b)))
    dump(output / "isolation.json", isolation)
    checks.append(dict(id="ISOLATION", status="PASS" if all(isolation.values()) else "FAIL"))
    # 3. C2 仅用户明确更正，经过 Production extraction/parser/提交。
    c2_workspace = output / "C2/workspace"
    shutil.copytree(original, c2_workspace)
    engine = AgentEngine.restore(config(), c2_workspace, session_id)
    corrected = run(engine, "我明确更正 Nerul-X7 的当前端口，替代旧记录：" + NEW,
                    OfflineClient([text_response("更正已接收")]), output / "C2/execution")
    records = engine.session.context_records.records
    c2 = any(r.content == NEW and r.status == "active" for r in records) and any(r.record_id == old_id and r.status == "superseded" for r in records)
    query = run(engine, "Nerul-X7 当前端口是什么？", OfflineClient([text_response("59263")]), output / "C2-query")
    c2 = c2 and NEW in primary_requests(query)[0]["instructions"] and OLD not in primary_requests(query)[0]["instructions"]
    checks.append(dict(id="C2_EXPLICIT", status="PASS" if c2 else "FAIL", automatic_tool_supersede="CAPABILITY_GAP"))
    # 4. C5-A/B 从同一旧事实 baseline 克隆，不改写 Checkpoint。
    for variant in ("C5-A", "C5-B"):
        clone = output / variant / "workspace"
        shutil.copytree(original, clone)
        active = AgentEngine.restore(config(), clone, session_id)
        pause_prompt = "继续 Nerul-X7 任务。请先 glob。"
        if variant == "C5-B":
            pause_prompt += "暂停后按我的明确授权更正为：" + NEW
        paused = run(active, pause_prompt,
                     OfflineClient([text_response(output=[tool_call("pause-probe", "glob", '{"pattern":"*.absent"}')])]), output / variant / "pause", pause=True)
        checkpoints = [r for r in active.session.store.read_transcript() if r["type"] == "checkpoint_committed"]
        checkpoint = CheckpointStore(active.session.store.session_dir).read(checkpoints[-1]["payload"]["checkpoint_id"])
        dump(output / variant / "checkpoint.json", asdict(checkpoint))
        cached = [r["record_id"] for r in checkpoint.run_state["relevant_context"]]
        if variant == "C5-B":
            sources = [r["sequence"] for r in active.session.store.read_transcript() if r["type"] == "context_user_query_appended" and NEW in str(r["payload"])]
            active.upsert_context_records([ContextRecordInput(NEW, "fact", (sources[-1],), (old_id,))])
        restored = AgentEngine.restore(config(), clone, active.session.store.session_id)
        authority = [asdict(r) for r in restored.session.context_records.records]
        reachable = restored.has_continuable_run
        result = run(restored, RunRequest.continue_run(), OfflineClient([text_response("继续完成")]), output / variant / "continue") if reachable else None
        instructions = primary_requests(result)[0]["instructions"] if result else ""
        expected = OLD if variant == "C5-A" else NEW
        valid = reachable and old_id in cached and result["terminal"]["status"] == "completed" and expected in instructions and (variant == "C5-A" or OLD not in instructions)
        evidence = dict(id=variant, status="PASS" if valid else "FAIL", formal_continue_reachable=reachable,
                        same_session=restored.session.store.session_id == active.session.store.session_id,
                        checkpoint_cached_ids=cached, restored_authority=authority,
                        old_fact_in_record_injection=OLD in instructions, correction_path="TRUSTED_ENGINE_API_WITH_EXPLICIT_USER_SOURCE" if variant == "C5-B" else "NONE")
        dump(output / variant / "oracle.json", evidence)
        checks.append(evidence)
        if not valid:
            break
    report = dict(status="PASS" if all(c["status"] == "PASS" for c in checks) else "NO_GO",
        evidence_type="DETERMINISTIC_MECHANISM_EVIDENCE", checks=checks,
        production_unchanged=before_source == source_fingerprint(),
        m1_m2_unchanged=all(tree_hash(Path(p)) == h for p,h in frozen.items()),
        paid_model_requests=0, limits=["FakeModel 不证明真实模型事实理解", "C5-B 更正使用受信 Engine API，不声称自动冲突替代", "Summary未触发；保留空Summary状态，不宣称覆盖Compaction"])
    dump(output / "report.json", report)
    print(json.dumps(dict(status=report["status"], report=str(output / "report.json")), ensure_ascii=False))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("eval/memory_m3/runs") / ("gate-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")))
    run_gate(parser.parse_args().output_dir.resolve())
