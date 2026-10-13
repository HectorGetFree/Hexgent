"""Production v2 集成的离线证据与收口，不提供真实模型入口。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from hashlib import sha256

ROOT=Path(__file__).resolve().parents[2]

def hashes():
    scopes=['eval/context_remediation_v1/runs','eval/context_remediation_v1/diagnostics','eval/context_management','eval/memory_m1/runs','eval/memory_m2/runs']
    return {str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for scope in scopes for p in sorted((ROOT/scope).rglob('*')) if p.is_file() and '__pycache__' not in p.parts}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');args=parser.parse_args()
    out=ROOT/'eval/context_remediation_v1/production_v2_closure'
    if args.prepare:
        out.mkdir(exist_ok=False)
        (out/'before.json').write_text(json.dumps(dict(created_at=datetime.now(timezone.utc).isoformat(),historical_hashes=hashes(),
            production_hashes={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hexgent').rglob('*.py')}),indent=2))
        print('HISTORICAL_ASSETS_FROZEN');return
    before=json.loads((out/'before.json').read_text())
    from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS,EXTRACTION_PROMPT_VERSION
    target=ROOT/'eval/context_remediation_v1/runs/extractor-quality-pilot-v1-20261012/requests/10.json'
    original=json.loads(target.read_text())['instructions']
    assert EXTRACTION_INSTRUCTIONS==original
    commands=[['.venv/bin/python','-m','unittest','tests.test_extractor_prompt_v2_integration','tests.test_key_fact_remediation','tests.test_context_compaction','tests.test_request_logprobs_compatibility','-q'],
        ['.venv/bin/python','-m','unittest','discover','-s','tests','-q'],
        ['.venv/bin/python','-m','unittest','eval.test_memory_m1','eval.test_memory_m2_real','eval.test_memory_hardening','eval.phase3.test_recovery_contract','eval.recovery_hardening.test_hardening','eval.memory_m3.test_real_smoke','-q'],['git','diff','--check']]
    rows=[]
    for i,command in enumerate(commands):
        run=subprocess.run(command,cwd=ROOT,capture_output=True,text=True)
        (out/f'tests-{i}.txt').write_text(run.stdout+run.stderr)
        rows.append(dict(command=command,exit_code=run.returncode,evidence=f'tests-{i}.txt'))
    after=hashes();assert before['historical_hashes']==after
    passed=all(r['exit_code']==0 for r in rows)
    result=dict(integration_gate='C2_PRODUCTION_INTEGRATION_PASS' if passed else 'C2_PRODUCTION_INTEGRATION_FAIL',
        phase_gate='C2_PHASE_CLOSED_WITH_KNOWN_LIMITATIONS' if passed else 'NOT_CLOSED',prompt_version=EXTRACTION_PROMPT_VERSION,
        prompt_sha256=sha256(EXTRACTION_INSTRUCTIONS.encode()).hexdigest(),prompt_exact_match=True,historical_assets_unchanged=True,
        historical_asset_count=len(after),model_calls=0,network_calls=0,regressions=rows)
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    (out/'integrity.json').write_text(json.dumps(dict(before=before['historical_hashes'],after=after,unchanged=True),indent=2))
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
