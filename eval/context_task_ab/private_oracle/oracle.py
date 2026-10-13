"""外部确定性Oracle；禁止克隆到Agent工作区。"""
import json
from pathlib import Path
from eval.context_task_ab.harness import FIXTURE, manifest

FACTS = {
 'aster': (108000,3000000,.04,18000,14000,0,0,8,[]),
 'birch': (90000,2600000,.02,20000,36000,9000,12000,10,[]),
 'cobalt': (96000,2600000,.02,16000,12000,0,0,6,['residency']),
 'delta': (132000,4000000,0,20000,18000,18000,0,12,['budget','deadline']),
}
SOURCE_FILES = ['commercial.md','capacity.md','identity-and-support.md','security.md','delivery.md']

def expected_cost(row):
    annual,quota,over,onboard,connector,support,sso,_,_=row
    return int(2*(annual+support+sso)+onboard+connector+2*max(0,3600000-quota)*over)

def assess(workspace):
    """业务、Schema、证据、只读边界分别判定；不接受Agent自评。"""
    checks=[]
    def check(name,ok,detail=None):checks.append(dict(name=name,passed=bool(ok),detail=detail))
    frozen=manifest(FIXTURE);current=manifest(workspace)
    changed=[p for p,h in frozen.items() if current.get(p)!=h]
    extra=[p for p in current if p not in frozen and not p.startswith(('outputs/','.hexgent/'))]
    check('read_only_inputs',not changed and not extra,dict(changed=changed,unexpected=extra))
    try:
        data=json.loads((workspace/'outputs/comparison.json').read_text())
    except (ValueError,OSError) as exc:
        check('json',False,str(exc));return dict(oracle_pass=False,checks=checks)
    check('json',isinstance(data,dict))
    if not isinstance(data,dict):return dict(oracle_pass=False,checks=checks)
    for key,value in [('project_id','RIDGE-31'),('horizon_years',2),('annual_requests',3600000),('recommended','aster'),('fallback','birch')]:
        check(key,type(data.get(key)) is type(value) and data.get(key)==value)
    rows=data.get('vendors');valid=isinstance(rows,list) and len(rows)==4 and all(isinstance(r,dict) for r in rows)
    check('vendor_schema',valid)
    if valid:
        check('vendor_set',sorted(str(r.get('id')) for r in rows)==sorted(FACTS))
        for r in rows:
            ident=r.get('id')
            if not isinstance(ident,str) or ident not in FACTS:continue
            fact=FACTS[ident]
            for key,value in [('total_2y_cny',expected_cost(fact)),('eligible',not fact[-1]),('delivery_weeks',fact[-2])]:
                check(ident+':'+key,type(r.get(key)) is type(value) and r.get(key)==value)
            check(ident+':blockers',isinstance(r.get('blockers'),list) and all(isinstance(x,str) for x in r['blockers']) and sorted(r['blockers'])==sorted(fact[-1]))
            refs=r.get('evidence');paths=set();supported=True;quotes={}
            if not isinstance(refs,list):supported=False;refs=[]
            for ref in refs:
                if not isinstance(ref,dict):supported=False;continue
                path=ref.get('path');quote=ref.get('quote')
                if not isinstance(path,str) or path not in frozen or not isinstance(quote,str) or len(quote.strip())<8:
                    supported=False;continue
                supported &= quote in (FIXTURE/path).read_text()
                paths.add(path)
                quotes[path]=quotes.get(path,'')+'\n'+quote
            check(ident+':evidence',supported and all(f'vendors/{ident}/{f}' in paths for f in SOURCE_FILES))
            # 引文存在不等于支持关键结论，逐类检查当前合同锚点。
            anchors={
                'commercial.md':[f'年度平台许可{fact[0]}元',f'一次性实施{fact[3]}元',f'一次性连接器{fact[4]}元'],
                'capacity.md':[f'每年包含{fact[1]}次',f'每请求{fact[2]}元'],
                'identity-and-support.md':['支持OIDC',f'附加费为{fact[6]}元',f'支持费为{fact[5]}元'],
                'security.md':['审计保留365天','备份位于新加坡' if ident=='cobalt' else '全部位于中国大陆'],
                'delivery.md':[f'完整交付工期{fact[-2]}周'],
            }
            check(ident+':critical_source_support',all(all(a in quotes.get(f'vendors/{ident}/{f}','') for a in values) for f,values in anchors.items()))
    try:text=(workspace/'outputs/recommendation.md').read_text()
    except OSError:text=''
    check('markdown_contract',all(('## '+s) in text for s in ['推荐','备选','成本与条件','待核验','来源']) and 'aster' in text and 'birch' in text)
    return dict(oracle_pass=all(c['passed'] for c in checks),checks=checks)
