"""Phase5C-1 每次执行的可审计 observation artifact。"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path

from .observation import RuntimeObservation
from .oracles import OracleResult


@dataclass(frozen=True)
class FeasibilityArtifact:
    """把模型终态、逐 call 诊断、工作区 diff 与外部 oracle 一起保存。"""

    task_id: str
    agent_completed: bool
    runtime: RuntimeObservation
    oracle: OracleResult
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "agent_completed": self.agent_completed,
            "runtime": self.runtime.to_dict(),
            "workspace_diff_summary": list(self.oracle.changed),
            "oracle": {
                "state": self.oracle.state,
                "checks": self.oracle.checks,
                "detail": self.oracle.detail,
            },
            "metadata": self.metadata,
        }


def write_artifact(destination: Path, artifact: FeasibilityArtifact) -> Path:
    """使用独立 JSON artifact，避免 success 被 Agent 自述或聚合布尔值掩盖。"""

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination
