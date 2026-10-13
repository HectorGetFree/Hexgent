"""人工追加授权：仅复用冻结11请求一次，Eval传输超时300秒。"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json

from .offline import ROOT,save
from .factorial_pilot import check_integrity,file_hashes,execute


def main():
    parent=ROOT/"eval/context_remediation_v1/runs/extractor-quality-pilot-v1-20261012"
    output=ROOT/"eval/context_remediation_v1/runs"/("extractor-quality-pilot-v1-retry-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    manifest=json.loads((parent/"manifest.json").read_text())
    check_integrity(parent,manifest)
    parent_before=file_hashes(parent)
    output.mkdir()
    for name,digest in manifest["frozen_files"].items():
        data=(parent/name).read_bytes()
        assert sha256(data).hexdigest()==digest
        destination=output/name
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(data)
    amended=deepcopy(manifest)
    amended.update(order=["11"],total_call_limit=1,parent_run=str(parent),
        parent_artifact_hashes=parent_before,authorization="用户批准只尝试11一次，传输超时300秒；00/10/01不重跑",
        timeout_seconds=300.0,
        transport_configuration_revision=dict(field="timeout_seconds",previous=manifest["timeout_seconds"],current=300.0,
            scope="Eval SDK选项及流消费截止时间；冻结config.json和Provider请求正文不变"),
        previous_10_failure_preserved=True,created_at=datetime.now(timezone.utc).isoformat(),
        supplemental_budget_ceiling=sum(b["total_estimated_ceiling"] for b in manifest["budgets"] if b["arm"]=="11"))
    save(output/"manifest.json",amended)
    print(json.dumps(dict(output=str(output),order=amended["order"],timeout_seconds=amended["timeout_seconds"],estimated_ceiling=amended["supplemental_budget_ceiling"]),ensure_ascii=False),flush=True)
    execute(output,order=amended["order"])
    save(output/"parent-integrity.json",dict(unchanged=file_hashes(parent)==parent_before,before=parent_before,after=file_hashes(parent)))


if __name__=="__main__":
    main()
