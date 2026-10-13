"""一次离线检查；最终人工审核单独记录，不从匹配率推出。"""
import copy
import hashlib
import json
import runpy
import subprocess
from prepare_v3 import OUT, BASE, ROOT, read, save, hashes

def main():
    manifest=read(OUT/'manifest.json')
    assert all(hashlib.sha256((OUT/p).read_bytes()).hexdigest()==h for p,h in manifest['frozen_files'].items())
    old=OUT.parent/'reference_rules_adversarial'
    assert hashes(old)==manifest['v1_hashes'] and hashes(BASE)==manifest['v2_hashes']
    evaluate=runpy.run_path(str(old/'checker.py'))['evaluate']
    cases=read(OUT/'projection_v3_cases.json')
    variants=read(BASE/'a3_these_regression.json')
    local=read(OUT/'h_pilot11_f4_regression.json')
    expected=read(OUT/'expected_results_v3.json')
    rows=[]; byid={}
    for case in cases+variants+local:
        computed=evaluate(case)
        row=dict(case_id=case['case_id'],original_fact=case['content'],category=case['category'],computed=computed,
                 expected=expected[case['case_id']],test_verdict='PASS' if computed['classification']==expected[case['case_id']]['classification'] else 'FAIL')
        rows.append(row);byid[case['case_id']]=row
    save(OUT/'test_results.json',dict(original49_matched=sum(r['test_verdict']=='PASS' for r in rows[:49]),v2_8_matched=sum(r['test_verdict']=='PASS' for r in rows[49:57]),
        local_total=len(local),local_matched=sum(r['test_verdict']=='PASS' for r in rows[57:]),rows=rows,manual_verdict_not_inferred=True))
    historical=[r for r in rows[:49] if r['category']=='HISTORICAL_REGRESSION']
    controls=[r for r in rows[:49] if r['category']=='HISTORICAL_POSITIVE_CONTROL']
    save(OUT/'historical_regression_results.json',dict(historical_gaps=historical,positive_controls=controls))
    # 原13关系表达式原样复用，只将被查询结果替换为本轮实际执行结果。
    source=(BASE/'run_v2.py').read_text()
    code=source[source.index('    checks=['):source.index('    save(out/"consistency_report.json"')]
    env=dict(cls=lambda cid:byid[cid]['computed']['classification'],c=lambda cid:byid[cid]['computed'],historical=historical,controls=controls)
    exec(code.strip(),env,env)
    checks=env['checks']
    save(OUT/'consistency_report.json',dict(original_relation_count=13,passed=sum(v for _,v in checks),checks=[dict(name=n,status='PASS' if v else 'FAIL') for n,v in checks],
        target_subclaims=byid['H-Pilot11-f4']['computed']['subclaim_reviews'],exact_state_not_general_entailment=True))
    unchanged=all(a==b for a,b in zip(cases,read(BASE/'projection_v2_cases.json')) if a['case_id']!='H-Pilot11-f4')
    asset_checks={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest['historical_hashes'].items()}
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    integrity=dict(v1_unchanged=hashes(old)==manifest['v1_hashes'],v2_unchanged=hashes(BASE)==manifest['v2_hashes'],cross_unchanged=hashes(OUT.parent/'cross_run_reference_audit')==manifest['cross_hashes'],
        production_unchanged=hashes(ROOT/'hexgent')==manifest['production_hashes'],other48_identical=unchanged,historical_assets_unchanged=all(asset_checks.values()),
        historical_asset_checks=asset_checks,expectations_unchanged=all(hashlib.sha256((OUT/p).read_bytes()).hexdigest()==h for p,h in manifest['frozen_files'].items()),
        model_calls=0,network_calls=0,git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr))
    save(OUT/'integrity_report.json',integrity)
    print(json.dumps(dict(original49=rows[:49] and sum(r['test_verdict']=='PASS' for r in rows[:49]),v2_8=sum(r['test_verdict']=='PASS' for r in rows[49:57]),local=sum(r['test_verdict']=='PASS' for r in rows[57:]),relations=sum(v for _,v in checks),failures=[r['case_id'] for r in rows if r['test_verdict']=='FAIL']),ensure_ascii=False))

if __name__=='__main__':main()
