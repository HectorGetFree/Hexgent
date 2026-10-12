"""序列化修复准入：禁网络、隔离凭证并保留独立证据。"""
from pathlib import Path
from datetime import datetime,timezone
from unittest.mock import patch
from contextlib import ExitStack
import io
import os
import socket
import unittest

from eval.context_management.c1_independent import hashes,save
from eval.context_management.c2_prep_3.prepare import ROOT,digest,snapshot


def main():
    before=hashes();protected=snapshot()
    frozen={str(p):digest(p) for p in ROOT.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
    failed=ROOT.parent/'c2/runs/stage1-real-20261011T090900010334Z'
    original={str(p):digest(p) for p in failed.rglob('*') if p.is_file()}
    output=ROOT.parent/'c2/runs'/('serialization-repair-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir();network=[];log=io.StringIO()
    def deny(*a,**kw):network.append('attempt');raise AssertionError('禁止网络')
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ,{},clear=True))
        stack.enter_context(patch.object(socket.socket,'connect',deny))
        stack.enter_context(patch('socket.create_connection',deny))
        modules=['eval.context_management.c2.test_serialization','eval.context_management.c2_dispatch_guard_repair.test_guard',
            'eval.context_management.c2_prep_3.test_gate','eval.context_management.c2_prep_2.test_control',
            'eval.context_management.c2_prep.test_prepare','eval.tool_hardening.test_v3_provider']
        tests=unittest.TextTestRunner(stream=log,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(modules))
    (output/'test_results.txt').write_text(log.getvalue(),encoding='utf-8')
    unchanged=lambda group:all(Path(p).exists() and digest(Path(p))==h for p,h in group.items())
    result=dict(tests=tests.testsRun,failures=len(tests.failures),errors=len(tests.errors),skips=len(tests.skipped),
        production_unchanged=before==hashes(),frozen_unchanged=unchanged(frozen),protected_unchanged=protected==snapshot(),
        original_failed_run_unchanged=unchanged(original),real_calls=0,network_attempts=network,output=str(output))
    result['gate']='PASS' if tests.wasSuccessful() and all(result[k] for k in ['production_unchanged','frozen_unchanged','protected_unchanged','original_failed_run_unchanged']) and not network else 'FAIL'
    save(output/'result.json',result);print(result)
    if result['gate']!='PASS':raise RuntimeError('SERIALIZATION_GATE_FAILED')


if __name__=='__main__':main()
