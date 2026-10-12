"""记录本轮测试和最终独立复核；保留先前Dry Run。"""
from pathlib import Path
import io
import unittest
from .runner import main as dry_run
from .prepare import ROOT,freeze
from eval.context_management.c1_independent import save


def main():
    if (ROOT/'test_results.txt').exists() or (ROOT/'final_dry_run.reviewed.json').exists():
        raise RuntimeError('不覆盖已有验收日志')
    suite=unittest.defaultTestLoader.loadTestsFromNames([
        'eval.context_management.c2_prep_3.test_gate',
        'eval.context_management.c2_prep_2.test_control',
        'eval.context_management.c2_prep.test_prepare',
        'eval.tool_hardening.test_v3_provider'])
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    (ROOT/'test_results.txt').write_text(stream.getvalue())
    if not result.wasSuccessful():raise RuntimeError('确定性回归失败')
    final=dry_run()
    final['offline_regression']=dict(total=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped))
    freeze(ROOT/'final_dry_run.reviewed.json',final)


if __name__=='__main__':main()
