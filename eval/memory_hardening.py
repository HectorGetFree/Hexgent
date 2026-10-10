"""Memory 真实模型评测：准备隔离工作区、离线判定；绝不调用模型。"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import shlex
import tempfile
import re

from hexgent.memory.memory_store import _parse_item
from hexgent.observation import source_fingerprint, summarize
from hexgent.config import load_config


CASES = {
    "project": {
        "category": "project",
        "a": "北辰项目的发布步骤固定依次为：静态检查、候选部署、验收签字。请为后续会话长期记住这个项目约定。",
        "b": '请按北辰项目的发布约定给我发布清单。只返回 JSON：{"project":"北辰","steps":[步骤名称]}，steps 按执行顺序排列，不加代码块。',
        "facts": ["北辰", "静态检查", "候选部署", "验收签字"],
        "expected": {"project": "北辰", "steps": ["静态检查", "候选部署", "验收签字"]},
    },
    "preference": {
        "category": "preference",
        "a": "我希望澄海项目的评审结果始终按四个小节呈现：风险、证据、建议、待确认，且严格保持这个顺序。请长期记住我的评审呈现偏好，供以后会话使用。",
        "b": '澄海项目评审：本次变更涉及缓存失效，尚缺回滚演练记录。请按我的评审呈现偏好列出小节标题。只返回 JSON：{"project":"澄海","headings":[小节标题]}，不加代码块。',
        "facts": ["澄海", "风险", "证据", "建议", "待确认"],
        "expected": {"project": "澄海", "headings": ["风险", "证据", "建议", "待确认"]},
    },
    "constraint": {
        "category": "constraint",
        "a": "青禾项目的发布通知只能发送到工单评论区，禁止通过邮件通知。请为后续会话长期记住这个项目约束。",
        "b": '青禾项目准备发布，请依据项目约束安排发布通知。只返回 JSON：{"project":"青禾","channel":通知渠道,"email_allowed":布尔值}，不加代码块。',
        "facts": ["青禾", "工单评论区", "邮件"],
        "expected": {"project": "青禾", "channel": "工单评论区", "email_allowed": False},
    },
}


def _write_json(path, value):
    """只新增评测产物，不覆盖已有证据。"""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def prepare(output_dir):
    """空白工作区与答案/提示文件分离，不复制用户真实 Memory。"""
    repo = Path(__file__).resolve().parents[1]
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise ValueError("输出目录已存在，请使用新的 run 目录")
    # 1. 配置沿用当前生产配置，仅明确本次已授权的自动审批模式。
    config = (repo / "hexgent.toml").read_text(encoding="utf-8")
    if "[permission]" in config:
        raise ValueError("现有配置含 permission section，请先明确自动审批配置，避免重复覆盖")
    config += '\n[permission]\ndefault_mode = "auto_approve"\n'
    output_dir.mkdir(parents=True)
    workspace_root = Path(tempfile.mkdtemp(prefix="hexgent-memory-eval-"))
    manifest = {
        "revision": "memory-real-v1", "evidence_type": "REAL_MODEL_BEHAVIORAL_EVIDENCE",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repo": str(repo), "workspace_root": str(workspace_root),
        "runtime_sha256": source_fingerprint(), "cases": {},
        "limitations": ["三组单次样本不代表泛化成功率", "无 OFF 对照，不能证明因果收益", "不是 OS sandbox"],
    }
    for name, case in CASES.items():
        workspace = workspace_root / name
        workspace.mkdir()
        # 2. 确保 CLI 不向上寻找并误用仓库工作区及其 Memory。
        (workspace / "pyproject.toml").write_text('# Memory Eval 独立工作区标记\n', encoding="utf-8")
        (workspace / "hexgent.toml").write_text(config, encoding="utf-8")
        for stage in ("a", "b"):
            (output_dir / f"{name}-{stage}.txt").write_text(case[stage] + "\n", encoding="utf-8")
        manifest["cases"][name] = {**case, "workspace": str(workspace),
                                    "config_sha256": sha256(config.encode()).hexdigest(),
                                    "effective_config": asdict(load_config(workspace))}
    _write_json(output_dir / "manifest.json", manifest)
    commands = ["# 每组先 A 后 B；两次分别启动 CLI，完成后 exit，不使用 /restore。",
                "# 使用当前生产 Provider 配置；仓库 .env 可能覆盖 export，按 Provider 观测确认实际模型。"]
    for name, case in manifest["cases"].items():
        launch = (f"cd {shlex.quote(case['workspace'])}\n"
                  f"PYTHONPATH={shlex.quote(str(repo))} {shlex.quote(str(repo / '.venv/bin/python'))} -m hexgent")
        commands.extend([f"\n# {name} Session A：将下面显示的内容粘入 CLI，空行发送，完成后 exit。",
                         f"cat {shlex.quote(str(output_dir / (name + '-a.txt')))}", launch,
                         f"\n# {name} Session B：重新启动，粘入 B 提示，完成后 exit。",
                         f"cat {shlex.quote(str(output_dir / (name + '-b.txt')))}", launch])
    (output_dir / "commands.txt").write_text("\n".join(commands) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(output_dir / "manifest.json"),
                      "commands": str(output_dir / "commands.txt"),
                      "workspace_root": str(workspace_root)}, ensure_ascii=False))


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _payloads(rows, kind):
    return [r["payload"] for r in rows if r.get("type") == kind]


def _session(path, workspace, query):
    """只接受该独立工作区的一次新 Session / 单次任务，不截取漂亮的 Run。"""
    path = path.resolve()
    if path.parent != (workspace / ".hexgent/sessions").resolve():
        raise ValueError("Session 不属于指定评测工作区")
    rows = _rows(path / "transcript.jsonl")
    starts = _payloads(rows, "session_started")
    runs = _payloads(rows, "agent_run_started")
    if len(starts) != 1 or starts[0].get("first_query") != query or len(runs) != 1:
        raise ValueError("必须使用指定提示的新 Session，且只执行一次任务")
    observations = _rows(path / "observation.jsonl") if (path / "observation.jsonl").exists() else []
    finished = _payloads(rows, "agent_run_finished")
    terminal = finished[-1] if finished else {}
    return rows, observations, starts[0]["session_id"], terminal


def _facts_match(case, content):
    """检查完整文本锚点、顺序及禁止语义，避免记反约束仍判为成功。"""
    if not all(fact in content for fact in case["facts"]):
        return False
    ordered = case["expected"].get("steps", case["expected"].get("headings"))
    if ordered:
        return [content.index(fact) for fact in ordered] == sorted(content.index(fact) for fact in ordered)
    return bool(re.search(r"(?:禁止|不得|不能|不允许|不可|不通过|不使用)[^。；\n]{0,20}邮件", content))


def evaluate(case, a_rows, b_rows, a_obs, b_obs, items, a_terminal, b_terminal):
    """三层 Oracle 分开判定：工具持久化、成功请求注入、行为结果。"""
    remembered = [p for p in _payloads(a_rows, "tool_execution_finished")
                  if p.get("tool_name") == "remember" and p.get("status") == "success"]
    ids = {(p.get("data") or {}).get("memory_id") for p in remembered}
    gold = [i for i in items if i.memory_id in ids and i.source == "explicit"
            and _facts_match(case, i.content)]
    # 3. 必须对应一次成功 MAIN_AGENT 请求，失败/取消的请求不算有效注入。
    successful_spans = {r["span_id"] for r in b_obs
                        if r.get("event") == "provider_finished" and r.get("status") == "SUCCEEDED"}
    receipts = [r for r in b_obs if r.get("event") == "memory_injected"
                and r.get("request_type") == "MAIN_AGENT" and r.get("span_id") in successful_spans]
    expected_ids = {i.memory_id for i in gold}
    hashes = {i.memory_id: sha256(i.content.encode()).hexdigest() for i in gold}
    first = receipts[0] if receipts else None
    injected = {m["memory_id"] for m in first.get("memories", [])} if first else set()
    exact = {m["memory_id"] for m in first.get("memories", [])
             if hashes.get(m["memory_id"]) == m.get("content_sha256")} if first else set()
    try:
        answer = json.loads(b_terminal.get("output_text", ""))
        behavior = type(answer) is dict and answer.keys() == case["expected"].keys() and all(
            type(answer[k]) is type(v) and answer[k] == v for k, v in case["expected"].items())
    except (ValueError, TypeError):
        answer, behavior = None, False
    calls_b = _payloads(b_rows, "tool_execution_finished")
    # 当前 B 无需工具；若直接读取 Memory/Transcript，不能当作自动召回使用成功。
    attempted_tools_b = [r for r in b_rows if r.get("type") in {"tool_execution_started", "tool_execution_dispatched"}]
    behavior_guard = not calls_b and not attempted_tools_b
    completed = a_terminal.get("status") == b_terminal.get("status") == "completed"
    persistence = len(gold) == 1
    injection = bool(expected_ids) and exact >= expected_ids
    result = {
        "terminal_completed": completed,
        "persistence_oracle": "PASS" if persistence else "FAIL",
        "injection_oracle": "PASS" if injection else ("BLOCKED" if not receipts else "FAIL"),
        "behavior_oracle": "PASS" if behavior else "FAIL",
        "behavior_isolated_from_tool_reads": behavior_guard,
        "remember_call_count": len(remembered), "memory_ids": sorted(expected_ids),
        "memory_categories": [i.category for i in gold],
        "first_successful_request_receipt": first,
        "injection_recall": len(injected & expected_ids) / len(expected_ids) if expected_ids and first else None,
        "injection_precision": len(injected & expected_ids) / len(injected) if injected else None,
        "actual_answer": answer, "session_b_tool_calls": calls_b,
        "provider_observation_a": summarize(a_obs), "provider_observation_b": summarize(b_obs),
        "provider_failures": [r for r in a_obs + b_obs if r.get("event") == "provider_finished"
                              and r.get("status") != "SUCCEEDED"],
        "model_failures": _payloads(a_rows, "model_response_failed") + _payloads(b_rows, "model_response_failed"),
        "stage_a_terminal": a_terminal, "stage_b_terminal": b_terminal,
        "evidence_type": "REAL_MODEL_BEHAVIORAL_EVIDENCE",
    }
    result["status"] = "PASS" if all((completed, persistence, injection, behavior, behavior_guard)) else "NOT_PASS"
    return result


def analyze(output_dir, name, session_a, session_b):
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    case = manifest["cases"][name]
    workspace = Path(case["workspace"])
    a_rows, a_obs, a_id, a_terminal = _session(session_a, workspace, case["a"])
    b_rows, b_obs, b_id, b_terminal = _session(session_b, workspace, case["b"])
    if a_id == b_id:
        raise ValueError("A/B 必须是不同 Session，不能复用上下文")
    source_ok = all(any(r.get("event") == "source_snapshot" and
                       r.get("runtime_sha256") == manifest["runtime_sha256"] for r in obs)
                    for obs in (a_obs, b_obs))
    config_ok = sha256((workspace / "hexgent.toml").read_bytes()).hexdigest() == case["config_sha256"]
    effective_config_ok = all(any(r.get("event") == "run_configuration"
                                 and r.get("config") == case["effective_config"]
                                 and r.get("permission_mode") == "auto_approve"
                                 and r.get("intent") == "submit" for r in obs)
                              for obs in (a_obs, b_obs))
    providers = [{(r.get("provider_host"), r.get("model")) for r in obs
                  if r.get("event") == "provider_started" and r.get("request_type") == "MAIN_AGENT"}
                 for obs in (a_obs, b_obs)]
    provider_ok = len(providers[0]) == 1 and providers[0] == providers[1]
    ends_a = [r["created_at"] for r in a_rows if r.get("type") == "agent_run_finished"]
    chronology_ok = bool(ends_a) and datetime.fromisoformat(ends_a[-1].replace("Z", "+00:00")) < datetime.fromisoformat(b_rows[0]["created_at"].replace("Z", "+00:00"))
    items = [_parse_item(path) for path in sorted((workspace / ".hexgent/memory/items").glob("mem-*.md"))]
    report = evaluate(case, a_rows, b_rows, a_obs, b_obs, items, a_terminal, b_terminal)
    report.update(case=name, session_a=str(session_a.resolve()), session_b=str(session_b.resolve()),
                  source_snapshot_matches=source_ok, config_matches=config_ok,
                  effective_config_matches=effective_config_ok,
                  provider_model_matches=provider_ok,
                  provider_models=[sorted(p, key=str) for p in providers],
                  session_b_started_after_a_finished=chronology_ok,
                  limitation=manifest["limitations"])
    if not all((source_ok, config_ok, effective_config_ok, provider_ok, chronology_ok)):
        report["status"] = "NOT_PASS"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"{name}-report-{stamp}.json"
    _write_json(path, report)
    print(json.dumps({"status": report["status"], "report": str(path)}, ensure_ascii=False))
    return report, path


def analyze_all(output_dir):
    """自动关联六个 Session；重复尝试存在歧义时拒绝挑选成功样本。"""
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    results = []
    for name, case in manifest["cases"].items():
        try:
            candidates = {"a": [], "b": []}
            for session in sorted((Path(case["workspace"]) / ".hexgent/sessions").glob("*")):
                if not (session / "transcript.jsonl").is_file():
                    continue
                starts = _payloads(_rows(session / "transcript.jsonl"), "session_started")
                if len(starts) != 1:
                    raise ValueError("Session 起始事实无效")
                for stage in candidates:
                    if starts[0].get("first_query") == case[stage]:
                        candidates[stage].append(session)
            if any(len(paths) != 1 for paths in candidates.values()):
                raise ValueError("A/B 各需唯一 Session；缺失或重复尝试不能自动选择")
            report, path = analyze(output_dir, name, candidates["a"][0], candidates["b"][0])
            results.append({**report, "report_path": str(path)})
        except (OSError, ValueError, KeyError) as error:
            results.append({"case": name, "status": "BLOCKED", "artifact_error": str(error)})
    count = len(manifest["cases"])
    summary = {
        "revision": manifest["revision"], "planned_scenarios": count, "planned_sessions": count * 2,
        "cases": results, "limitations": manifest["limitations"],
        "status": "PASS" if all(r["status"] == "PASS" for r in results) else "NOT_PASS",
        "blocked_scenarios": sum(r["status"] == "BLOCKED" for r in results),
    }
    for metric in ("persistence_oracle", "injection_oracle", "behavior_oracle"):
        passed = sum(r.get(metric) == "PASS" for r in results)
        summary[metric] = {"pass": passed, "planned": count, "pass_fraction": passed / count,
                           "unavailable": sum(r.get(metric) in {None, "BLOCKED"} for r in results)}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = output_dir / f"summary-{stamp}.json"
    _write_json(path, summary)
    print(json.dumps({"status": summary["status"], "summary": str(path)}, ensure_ascii=False))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--output-dir", required=True, type=Path)
    audit = sub.add_parser("analyze")
    audit.add_argument("--output-dir", required=True, type=Path)
    audit.add_argument("--case", required=True, choices=CASES)
    audit.add_argument("--session-a", required=True, type=Path)
    audit.add_argument("--session-b", required=True, type=Path)
    all_cases = sub.add_parser("analyze-all")
    all_cases.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.output_dir)
    elif args.command == "analyze":
        analyze(args.output_dir.resolve(), args.case, args.session_a, args.session_b)
    else:
        analyze_all(args.output_dir.resolve())


if __name__ == "__main__":
    main()
