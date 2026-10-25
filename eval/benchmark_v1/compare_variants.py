"""只读比较同条件Variant Campaign；不执行Agent或重判历史文件。"""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from .baseline import collect,digest
from .protocol_v2 import strict_jsonl
from .variants import VARIANTS,effective_config
from dataclasses import asdict


def comparable(left,right):
    # 不混用不同Provider/Production/依赖/评分/环境和预算条件。
    for key in ('protocol_version','provider','runtime_source_sha256','build_metadata','source_hashes','tool_registry','tool_schemas'):
        if key not in left or left[key]!=right.get(key):raise ValueError('不可比Campaign：'+key)
    if left['protocol_version']!=2:raise ValueError('比较仅支持协议v2')
    if left.get('variant_id') not in VARIANTS or right.get('variant_id') not in VARIANTS:raise ValueError('缺少显式Variant；旧结果不自动升级')
    a={c['case_id']:c for c in left['inventory']};b={c['case_id']:c for c in right['inventory']}
    if a.keys()!=b.keys():raise ValueError('Inventory不一致')
    common=[]
    for cid,c in a.items():
        d=b[cid]
        for key in ('contract_sha256','baseline_sha256','budget'):
            if c[key]!=d[key]:raise ValueError('不可比Case：'+cid+'/'+key)
        for manifest,item in ((left,c),(right,d)):
            if item['full_config']!=asdict(effective_config(item['budget'],manifest['variant_id'])):raise ValueError('冻结配置超出Variant定义')
        if c['eligible'] and d['eligible']:common.append(cid)
    return sorted(common)


def load_campaign(root):
    root=Path(root).resolve();path=root/'baseline-manifest.json';m=json.loads(path.read_text())
    if digest(path)!=json.loads((root/'manifest-sha256.json').read_text())['sha256']:raise ValueError('Manifest摘要不匹配')
    if m.get('variant_id') not in VARIANTS:raise ValueError('需要显式Variant Campaign，历史Baseline保持独立')
    plan={t['trial_id']:t for t in m['trial_plan']};seen=set();checked=[]
    for r in strict_jsonl(root/'trial-records.jsonl'):
        tid=r['trial_id']
        if tid in seen or tid not in plan:raise ValueError('重复或未计划Trial')
        seen.add(tid);trial=plan[tid];case=next(c for c in m['inventory'] if c['case_id']==trial['case_id'])
        # 重新从可信原始Evidence评分/提取指标，不用聚合报告证明结果。
        if len(list((root/'trials'/tid).rglob('protocol-result-v2.json')))!=1:raise ValueError('缺少唯一协议v2结果；比较不使用旧写入路径')
        actual=collect(SimpleNamespace(root=root),case,trial,r['returncode'],r['total_wall_seconds'])
        for key in ('case_id','variant_id','task_status','protocol_status','valid_task_sample','effective_config_sha256','evidence_manifest_sha256','contract_sha256','safety_violation','termination_reason'):
            if actual.get(key)!=r.get(key):raise ValueError('Trial Record与原始证据不符：'+key)
        checked.append(actual)
    return m,checked


def summarize(records,case_ids):
    result=[]
    for cid in case_ids:
        rows=[r for r in records if r['case_id']==cid];valid=[r for r in rows if r['valid_task_sample']]
        n=len(valid);passed=sum(r['task_status']=='PASS' for r in valid)
        result.append(dict(case_id=cid,attempts=len(rows),valid=n,passed=passed,failed=n-passed,
            excluded=len(rows)-n,pass_rate=passed/n if n else None,
            known_tokens=sum(r['metrics']['known_provider_tokens'] for r in valid if r['metrics'].get('known_provider_tokens') is not None) if any(r['metrics'].get('known_provider_tokens') is not None for r in valid) else None,
            usage_unknown=any(r['metrics'].get('known_provider_tokens') is None or 'UNKNOWN' in r['metrics'].get('usage_coverage','') for r in valid)))
    return result


def compare(roots):
    loaded=[load_campaign(p) for p in roots]
    if len(loaded)<2:raise ValueError('至少需要两个Campaign')
    variants=[m['variant_id'] for m,_ in loaded]
    if len(set(variants))!=len(variants):raise ValueError('每个Variant只接受一个Campaign，禁止静默挑选/拼接')
    if 'baseline' not in variants:raise ValueError('必须提供baseline')
    baseline=next(m for m,_ in loaded if m['variant_id']=='baseline')
    common=set(c['case_id'] for c in baseline['inventory'] if c['eligible'])
    for m,_ in loaded:common &= set(comparable(baseline,m))
    if not common:raise ValueError('没有共同适用Case')
    return dict(schema_version=1,matched_case_ids=sorted(common),
        groups=[dict(variant_id=m['variant_id'],campaign_id=m['campaign_id'],cases=summarize(r,sorted(common)),
            planned_attempts=len(m['trial_plan']),completed_attempts=len(r),complete=len(r)==len(m['trial_plan'])) for m,r in loaded],
        limitations=['只比较共同适用Case；NOT_APPLICABLE不作FAIL','n=3，不宣称统计显著性或因果效应已证实','未报告请求与Workspace Embedding成本未知；已知Token不是完整计费成本'])


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('campaigns',nargs='+',type=Path);args=p.parse_args()
    print(json.dumps(compare(args.campaigns),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
