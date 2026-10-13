"""V3 局部离线准备；不导入 Provider，不执行评分，不覆盖历史。"""
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/Users/hector/dev/hexgent')
OUT = Path(__file__).resolve().parent
BASE = OUT.parent / 'fixture_semantic_projection_v2'
OBJECTS = ['failure_recovery', 'permission_handling', 'other_runtime_behaviors']
ATTR = 'verification_epistemic_state_from_available_evidence'

def read(p):
    return json.loads(p.read_text())

def save(p, obj):
    if p.exists():
        if read(p) == obj:
            return
        raise RuntimeError(f'禁止覆盖：{p}')
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')

def hashes(p):
    return {str(f.relative_to(p)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(p.rglob('*')) if f.is_file() and '__pycache__' not in f.parts}

def req(obj, state='NOT_CONFIRMED_VERIFIED'):
    return dict(subjects=[obj], attribute=ATTR, value=state)

def main():
    cases = read(BASE / 'projection_v2_cases.json')
    original = copy.deepcopy(cases)
    target = next(c for c in cases if c['case_id'] == 'H-Pilot11-f4')
    target['claim_model'].extend(req(o) for o in OBJECTS)
    bridges = []
    for s in target['sources']:
        m = s.get('original_message', {})
        text = m.get('output', '')
        if m.get('type') != 'function_call_output':
            continue
        assert '它是阅读证据，不是本轮执行测试结果。' in text
        assert '失败恢复与权限结论需要独立证据。' in text
        atoms = [dict(subject=o, attribute=ATTR, value='NOT_CONFIRMED_VERIFIED') for o in OBJECTS]
        s['claims'].extend(atoms)
        bridge = dict(source_id=s['id'], raw_message_sha256=hashlib.sha256(json.dumps(m,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
            quotes=['它是阅读证据，不是本轮执行测试结果。', '断言存在不等于实际通过，失败恢复与权限结论需要独立证据。'],
            atoms=atoms, reasoning='限定为据这些冻结源码阅读记录不能确认运行验证；不是全世界未验证。其他运行时行为同样不能仅由静态正文确认。审查完成台账不等于执行验证。')
        s['evidence_projection_derivations_v3'] = [bridge]
        bridges.append(bridge)
    assert len(bridges) == 30
    assert all(a == b for a,b in zip(original,cases) if a['case_id'] != target['case_id'])
    expected = read(BASE / 'expected_results_v2.json')
    expected[target['case_id']] = dict(classification='REFERENCE_GAP', rationale='全前缀30阅读项支持主要构成；自身引用仅3项不足多数。后半限定静态阅读的认识能力，分别支持三个不可确认状态，不推出未发生。')
    variants = []
    def variant(cid, states, classification, content, unknown=False, extra=None):
        claims = [dict(subject=o, attribute=ATTR, value=v) for o,v in states.items()]
        src = dict(id='e',sequence=1,semantic_qualification='UNKNOWN' if unknown else 'CONFIRMED',raw_text=content,claims=claims)
        case = dict(case_id=cid,category='V3_LOCAL',content='现有证据不能确认三类运行时行为已验证。',sources=[src],source_refs=['e'],cutoff=2,claim_model=[req(o) for o in OBJECTS])
        if extra: case.update(extra)
        variants.append(case)
        expected[cid] = dict(classification=classification,rationale=content)
    for o in OBJECTS:
        variant('V3-only-'+o, {k:'VERIFIED' if k==o else 'NOT_CONFIRMED_VERIFIED' for k in OBJECTS}, 'STATE_ERROR', f'已提供{o}验证完成证据；另外两类经限定证据核查不能确认，不能互相替代。')
    variant('V3-all-verified',{o:'VERIFIED' for o in OBJECTS},'STATE_ERROR','三类均有明确验证完成记录。')
    variant('V3-no-confirmable',{o:'NOT_CONFIRMED_VERIFIED' for o in OBJECTS},'PASS','明确限定为阅读证据，三类均不能据此确认；不判断外部是否已执行。')
    variant('V3-explicit-not-verified',{o:'CONFIRMED_NOT_VERIFIED' for o in OBJECTS},'STATE_ERROR','证据明确确认三类尚未验证；原句仅为不能确认。这里检查精确认识状态投影，不提供跨状态蕴含规则。')
    explicit = copy.deepcopy(variants[-1]); explicit['case_id']='V3-explicit-matching'; explicit['content']='证据明确确认三类尚未验证。'; explicit['claim_model']=[req(o,'CONFIRMED_NOT_VERIFIED') for o in OBJECTS]
    variants.append(explicit); expected[explicit['case_id']]=dict(classification='PASS',rationale='明确否定的来源与明确否定的投影一致。')
    variant('V3-unknown',{o:'UNKNOWN' for o in OBJECTS},'UNCERTAIN','来源无法确定三类认识状态，不可升级为不能确认或明确未验证。',unknown=True)
    variant('V3-tests-only',{},'UNSUPPORTED_FACT','只有一般测试通过记录，未限定测试覆盖三类行为，也无三类认识状态核查。')
    variants[-1]['sources'][0]['claims']=[dict(subject='runtime_tests',attribute='passed',value=True)]
    gap=copy.deepcopy(target);gap['case_id']='V3-front-gap';gap['category']='V3_LOCAL';variants.append(gap)
    expected[gap['case_id']]=dict(classification='REFERENCE_GAP',rationale='前半范围缺口不免除后半三项独立判定。')
    # 明确未验证比不能确认更强；精确状态不等价，不宣称原自然语言弱命题在逻辑上为假。
    save(OUT/'projection_v3_cases.json',cases)
    save(OUT/'h_pilot11_f4_regression.json',variants)
    save(OUT/'expected_results_v3.json',expected)
    save(OUT/'h_pilot11_f4_projection_diff.json',dict(before=next(c for c in original if c['case_id']==target['case_id']),after=target,bridges=bridges,
        semantic_state_contract='局部精确状态标签，不是自然语言跨状态蕴含系统。CONFIRMED_NOT_VERIFIED可支持更弱的不确认，但不得无损等同；专项STATE_ERROR仅表示精确投影错配。'))
    save(OUT/'projection_fidelity_audit.json',dict(target_mapping=[dict(clause='主要是阅读证据',projection=target['claim_model'][0]),dict(clause='不能据此确认测试通过',projection=target['claim_model'][1])]+[dict(clause=o,projection=req(o),scope='据现有阅读记录',cutoff=120) for o in OBJECTS],bridges=bridges,other48_unchanged=True,final_manual_review_pending=True))
    old = OUT.parent/'reference_rules_adversarial'
    assert 'REFERENCE_RULES_ADVERSARIAL_FAIL' in (old/'final_review.md').read_text()
    assert read(BASE/'manual_review_findings.json')['manual_original49_pass']==48
    save(OUT/'manifest.json',dict(created_at=datetime.now(timezone.utc).isoformat(),old_gate='REFERENCE_RULES_ADVERSARIAL_FAIL',v2_gate=read(BASE/'manual_review_findings.json')['gate'],
        v1_hashes=hashes(old),v2_hashes=hashes(BASE),cross_hashes=hashes(OUT.parent/'cross_run_reference_audit'),
        production_hashes=hashes(ROOT/'hexgent'),historical_hashes=read(BASE/'manifest.json')['frozen_assets'],
        frozen_files=hashes(OUT),expected_frozen_before_tests=True,original_count=49,v2_variant_count=8,v3_variant_count=len(variants),model_calls=0,network_calls=0))
    print('PREPARED_EXPECTATIONS_FROZEN')

if __name__=='__main__': main()
