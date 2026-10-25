"""T08计划内进程切点证明；仅信任Runner监督记录，不补造原Run结束。"""
import json
from pathlib import Path


def planned_crash(root,manifest):
    root=Path(root);p=root/'supervisor-crash.json'
    if not p.exists():return None
    proof=json.loads(p.read_text());crash=json.loads((root/'crash-boundary.json').read_text())
    contract=json.loads((root/manifest.contract.path).read_text())
    if (manifest.task_id!='T08' or contract['scenario_config']['fault']!='crash' or
        proof['trial_id']!=manifest.trial_id or proof['session_id'] not in {s.session_id for s in manifest.sessions} or
        proof['returncode']!=-9 or proof['process_stopped'] is not True or
        proof['source']!='Runner child.kill/wait; fixture cut' or type(proof['transcript_sequence']) is not int or
        proof['transcript_sequence']<1 or proof['boundary']!='sqlite_transaction_committed_before_http_response' or
        crash['session_id']!=proof['session_id'] or crash['signal']!='SIGKILL' or crash['returncode']!=-9 or
        crash['boundary']!=proof['boundary'] or crash['committed_count_at_cut']!=1):raise ValueError('计划内崩溃证明不一致')
    return proof


def verify_recovery_sources(root,manifest,sealed):
    """独立核对Checkpoint和切点前日志；不信任汇总中的checksum_valid自述。"""
    proof=planned_crash(root,manifest)
    if proof is None:return
    from hexgent.session.checkpoint import CheckpointStore
    from hexgent.session.session_restore import _pending_run_state
    from .protocol_v2 import strict_jsonl
    root=Path(root);e=json.loads((root/'observations/remaining-evidence.json').read_text())
    for name,key in [('crash-boundary.json','crash'),('restore.json','restore'),('continue-result.json','continue_result')]:
        p=root/name
        if p.exists() and (name not in sealed or json.loads(p.read_text())!=e[key]):raise ValueError('恢复Evidence与原始来源不一致')
    info=e['checkpoint'];sid=proof['session_id'];cid=info['checkpoint_id']
    path=f'sessions/{sid}/checkpoints/{cid}.json'
    if path not in sealed:raise FileNotFoundError('恢复Checkpoint未封存')
    cp=CheckpointStore(root/'sessions'/sid).read(cid)
    if cp.session_id!=sid or cp.run_state!=info['run_state']:raise ValueError('Checkpoint身份/运行状态不匹配')
    rows=strict_jsonl(root/'sessions'/sid/'transcript.jsonl')
    prefix=[r for r in rows if r['sequence']<=proof['transcript_sequence']]
    if not prefix or prefix[-1]['sequence']!=proof['transcript_sequence']:raise ValueError('崩溃切点序号不存在')
    expected=_pending_run_state(cp,prefix);restore=e['restore']
    if restore.get('has_continuable_run'):
        if expected is None:raise ValueError('原始日志无法重建被声明的续跑状态')
        actual=restore['restored_run_state'];expected=expected.to_dict()
        if any(actual.get(k)!=v for k,v in expected.items() if k!='elapsed_seconds'):raise ValueError('Restore预算或控制状态未正确继承')
        if actual.get('elapsed_seconds',-1)<cp.run_state['elapsed_seconds']:raise ValueError('Restore耗时回退')
