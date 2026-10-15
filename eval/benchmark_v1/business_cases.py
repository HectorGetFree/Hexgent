"""B1-C 固定真实业务 Case；业务输入公开，Oracle 与阶段调度仅 Evaluator 可见。"""

from pathlib import Path
import json

from .contracts import TaskContract


CASES = Path(__file__).with_name("cases")
BUSINESS_TOOLS = {"search_workspace", "list_folder", "get_document", "create_document", "update_document"}
INTERNAL_TOOLS = {"read_file", "create_plan", "update_plan"}


def load_case(task_id: str, *, revision: str | None = None):
    if task_id not in {"T01", "T02", "T03"}:
        raise ValueError("仅支持 B1-C 的 T01 / T02 / T03")
    root = CASES / task_id.lower()
    if revision is not None:
        if revision != "f1":
            raise ValueError("仅支持显式 f1 Revision")
        root = root / "revisions" / revision
    contract = TaskContract.model_validate_json((root / "contract.json").read_text(encoding="utf-8"))
    baseline = json.loads((root / "baseline.json").read_text(encoding="utf-8"))
    if baseline["baseline_id"] != contract.baseline_id:
        raise ValueError("Case 与 Baseline 标识不一致")
    return contract, baseline
