"""B1-D 固定 Case 加载；不调度 Trial。"""
from pathlib import Path
import json
from .contracts import TaskContract

CASES = Path(__file__).with_name('remaining_cases')

CASE_NAMES = tuple(sorted(p.name for p in CASES.iterdir() if p.is_dir()))

def load_remaining(case, *, revision=None):
    if case not in CASE_NAMES: raise ValueError('只允许冻结的 B1-D Case 名称')
    root=CASES/case
    if revision is not None:
        if revision != 'f1' or case == 't05-multiidentity':
            raise ValueError('该Case没有可执行的F1 Revision')
        root=root/'revisions'/revision
    contract = TaskContract.model_validate_json((root/'contract.json').read_text())
    baseline = json.loads((root/'baseline.json').read_text())
    if baseline['baseline_id'] != contract.baseline_id: raise ValueError('Baseline 身份与合同不一致')
    return contract, baseline
