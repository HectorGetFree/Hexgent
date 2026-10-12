"""离线验证日志和 Gate 汇总；不改写首次运行证据。"""
import io
import json
from pathlib import Path
import unittest
from collections import Counter
from .runner import ROOT, main as dry_run, hashes, protected_hashes
from eval.context_management.c1_independent import save


def main():
    production,protected=hashes(),protected_hashes()
    stream=io.StringIO()
    suites=unittest.defaultTestLoader.loadTestsFromNames([
        'eval.context_management.c2_prep_2.test_control',
        'eval.context_management.c2_prep.test_prepare',
        'eval.tool_hardening.test_v3_provider'])
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suites)
    path=ROOT/'test_results.txt'
    if path.exists(): raise RuntimeError('不覆盖旧测试日志')
    path.write_text(stream.getvalue())
    dry_run()
    latest=sorted((ROOT/'runs').glob('dry-*'))[-1]
    report=json.loads((latest/'result.json').read_text())
    audits=[]
    for item in report['results']:
        data=json.loads((latest/item['id']/'evidence.json').read_text())
        frozen=json.loads((latest/item['id']/'frozen-state.json').read_text())
        comp=[c for c in data['calls'] if c['call_type']=='COMPACTION']
        maincalls=[c for c in data['calls'] if c['call_type']=='MAIN']
        facts=frozen['facts']
        source_check=[dict(fact_id=f['fact_id'],in_summary_input=f['quote'] in json.dumps(comp[0]['request']['input'],ensure_ascii=False),
                          in_post_history=f['quote'] in json.dumps(maincalls[0]['request']['input'],ensure_ascii=False))
                      for f in facts if f['channel']=='SUMMARY_REQUIRED']
        audits.append(dict(id=item['id'],source_channel_check=source_check,
            summary_accepted=data['context']['compaction_count']>0,
            compaction_events=[e for e in data['events'] if 'before_tokens' in e or 'after_tokens' in e or 'message' in e],
            default_threshold=item['default_threshold'], attribution=item['attribution']))
    save(ROOT/'reviewed_dry_run.json',dict(report=report,source_audits=audits,
        offline_tests=dict(run=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped)),
        production_unchanged=production==hashes(),protected_evidence_unchanged=protected==protected_hashes()))
    failed={t.id() for t,_ in result.failures+result.errors}
    skipped={t.id() for t,_ in result.skipped}
    save(ROOT/'budget_dry_run.json',dict(evidence_type='OFFLINE_FAKE_TEST_TARIFF',tests={
        t._testMethodName:('FAIL' if t.id() in failed else 'SKIP' if t.id() in skipped else 'PASS')
        for group in ('BudgetTests','TraceTests','ProductionTraceTests')
        for t in unittest.defaultTestLoader.loadTestsFromName('eval.context_management.c2_prep_2.test_control.'+group)},
        note='测试费率不是报价；测试日志为实际结果权威，未确认价格与输入上界时实时请求被拒绝',
        shared_budget_calls=report['shared_budget_calls'],reserved_input=report['shared_reserved_input'],reserved_output=report['shared_reserved_output']))
    if not result.wasSuccessful(): raise RuntimeError('离线测试失败，禁止进入真实实验')


if __name__=='__main__': main()
