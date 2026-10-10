"""Phase5C-1 外部 deterministic oracle；不依赖 Agent 的最终自然语言声明。"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable

from .fixtures import changed_paths, workspace_manifest


OracleState = str


@dataclass(frozen=True)
class OracleResult:
    state: OracleState
    checks: dict[str, bool]
    changed: tuple[str, ...]
    detail: str = ""


def evaluate(task_id: str, workspace: Path, before: dict[str, str]) -> OracleResult:
    handlers: dict[str, Callable[[Path, dict[str, str]], OracleResult]] = {
        "R1": _research_brief,
        "D2": _review_result,
        "W1": _workflow,
        "C1": _release_config,
        "T1": _technical_fix,
    }
    if task_id not in handlers:
        raise KeyError(f"{task_id} 需要阶段性 oracle，不能使用通用 evaluate")
    return handlers[task_id](workspace, before)


def _result(checks: dict[str, bool], before: dict[str, str], workspace: Path, detail: str = "") -> OracleResult:
    changed = tuple(sorted(changed_paths(before, workspace_manifest(workspace))))
    return OracleResult("ORACLE_PASS" if all(checks.values()) else "ORACLE_FAIL", checks, changed, detail)


def _research_brief(workspace: Path, before: dict[str, str]) -> OracleResult:
    path = workspace / "brief.md"
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    return _result({
        "created": path.exists(), "conclusion": "每周报告" in content,
        "market_attribution": "market-notes.md" in content,
        "interview_attribution": "field-interviews.md" in content,
        "timeline_conflict": "冲突" in content and "时间" in content,
        "only_allowed_change": changed_paths(before, workspace_manifest(workspace)) <= {"brief.md"},
    }, before, workspace)


def _review_result(workspace: Path, before: dict[str, str]) -> OracleResult:
    path = workspace / "review-result.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
    checks = {"valid_json": isinstance(payload, dict), "only_allowed_change": changed_paths(before, workspace_manifest(workspace)) <= {"review-result.json"}}
    if isinstance(payload, dict):
        checks.update({
            "schema": set(payload) == {"reviews"} and isinstance(payload.get("reviews"), list),
            "alice_approved": {"name": "alice", "decision": "approved", "reason": "score_at_least_80"} in payload.get("reviews", []),
            "bob_rejected": {"name": "bob", "decision": "rejected", "reason": "score_below_80"} in payload.get("reviews", []),
        })
    return _result(checks, before, workspace)


def _workflow(workspace: Path, before: dict[str, str]) -> OracleResult:
    state_path, handoff_path = workspace / "project-state.json", workspace / "handoff.md"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = None
    handoff = handoff_path.read_text(encoding="utf-8") if handoff_path.exists() else ""
    return _result({
        "state_valid": isinstance(state, dict),
        "handoff_ready": isinstance(state, dict) and state.get("phase") == "handoff_ready",
        "risk_acknowledged": isinstance(state, dict) and state.get("risk_status") == "acknowledged",
        "handoff_sections": all(item in handoff for item in ("## 当前状态", "## 已知风险", "## 下一步")),
        "only_allowed_change": changed_paths(before, workspace_manifest(workspace)) <= {"project-state.json", "handoff.md"},
    }, before, workspace)


def _release_config(workspace: Path, before: dict[str, str]) -> OracleResult:
    path = workspace / "release-config.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
    return _result({
        "valid_json": isinstance(payload, dict),
        "expected_values": payload == {"service": "catalog", "environment": "production", "port": 8080, "retry_limit": 5},
        "only_allowed_change": changed_paths(before, workspace_manifest(workspace)) <= {"release-config.json"},
    }, before, workspace)


def _technical_fix(workspace: Path, before: dict[str, str]) -> OracleResult:
    process = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=workspace, capture_output=True, text=True, timeout=20)
    return _result({
        "tests_pass": process.returncode == 0,
        "only_parser_changed": changed_paths(before, workspace_manifest(workspace)) <= {"app/parser.py"},
    }, before, workspace, (process.stdout + process.stderr)[-1_000:])
