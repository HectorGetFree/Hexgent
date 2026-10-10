"""简短自然审计任务的多对 ON/OFF；仅用户显式启动时请求模型。"""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean, median
import subprocess
import sys

from hexgent.client import get_client, get_model
from .v2_runner import ROOT, digest, stats
from .v3_experiment import project, save, source_hashes
from .v3_smoke import run_one

QUERY = '请帮我审计一下 `hexgent/tool` 模块的设计'


def manual_evidence(task, records, answer, workspace):
    """自由文本不套用旧 JSON 引用 Oracle；质量独立人工审核。"""
    reads = []
    successful = {r['payload']['call_id'] for r in records if r['type'] == 'tool_execution_finished'
                  and r['payload']['status'] == 'success'}
    for record in records:
        p = record['payload']
        if record['type'] == 'tool_execution_started' and p.get('call_id') in successful and p.get('tool_name') == 'read_file':
            arguments = json.loads(p['arguments']) if isinstance(p['arguments'], str) else p['arguments']
            reads.append({'call_id': p['call_id'], 'path': arguments.get('path'), 'sequence': record['sequence']})
    return {'evaluation_mode': 'MANUAL_REVIEW', 'mechanical_pass': None, 'task_oracle_pass': None,
            'semantic_review': 'PENDING_MANUAL_REVIEW', 'strict_citation_status': 'NOT_APPLICABLE',
            'successful_reads': reads, 'successful_read_paths': sorted({r['path'] for r in reads}),
            'answer_nonempty': bool(answer.strip()), 'quote_checks': [], 'missing_required_reads': [], 'errors': [],
            'limitation': '新自由文本任务不要求 JSON/指定问题；不得将完成或读取文件等同任务质量 PASS'}


def eval_hashes():
    """冻结本实验及共享观测实现，续跑时防止中途变更口径。"""
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted((ROOT/'eval/tool_hardening').glob('*.py'))}


def validate(manifest):
    """不请求Provider，只核验本地配置与源码快照。"""
    if source_hashes() != manifest['source_hashes'] or eval_hashes() != manifest['eval_hashes']:
        raise RuntimeError('SOURCE_OR_EVAL_CHANGED')
    client = get_client()
    if get_model() != manifest['model'] or str(client.base_url) != manifest['provider']:
        raise RuntimeError('PROVIDER_CONFIG_CHANGED')
    return client


def trajectory(artifact, directory):
    """只规范化隔离workspace前缀；保留参数、顺序和响应边界。"""
    workspace = str((directory/'workspace').resolve())
    return [[(c['name'], json.loads(c['arguments'].replace(workspace, '<WORKSPACE>')))
             for c in row['tool_calls']] for row in artifact['turns'] if row['request_type'] == 'MAIN_AGENT']


def summarize(pairs):
    """保留失败/未完成样本；有效配对另列，不悄悄筛除异常。"""
    summary = {'planned_pairs': len(pairs), 'completed_pairs': 0, 'valid_pairs': 0, 'excluded_pairs': [],
               'task_quality': 'PENDING_MANUAL_REVIEW',
               'limitation': '重复降低单样本偶然性但不消除网络、Provider和规划波动；小样本P95不代表稳定尾延迟'}
    valid = []
    for pair in pairs:
        if all(v in pair['runs'] for v in ('ON', 'OFF')):
            summary['completed_pairs'] += 1
            if any(len(pair['runs'][v].get('attempts', [])) > 1 for v in ('ON', 'OFF')):
                summary['excluded_pairs'].append({'pair': pair['pair'], 'reason': 'RETRIED_PAIR_SEPARATE_ONLY'})
            elif not any(pair['runs'][v]['metrics']['stop_reasons'] for v in ('ON', 'OFF')):
                valid.append(pair)
            else:
                summary['excluded_pairs'].append({'pair': pair['pair'], 'reason': 'STOP_REASONS',
                    'details': {v: pair['runs'][v]['metrics']['stop_reasons'] for v in ('ON', 'OFF')}})
        else:
            summary['excluded_pairs'].append({'pair': pair['pair'], 'reason': 'INCOMPLETE', 'observed_variants': list(pair['runs'])})
    summary['valid_pairs'] = len(valid)
    summary['retry_pairs'] = [{'pair': p['pair'], 'runs': p['runs']} for p in pairs
        if any(len(r.get('attempts', [])) > 1 for r in p['runs'].values())]
    for variant in ('ON', 'OFF'):
        runs = [(p['runs'][variant].get('attempts') or [{'metrics': p['runs'][variant]['metrics']}])[0]['metrics']
                for p in pairs if variant in p['runs']]
        summary[variant] = {'observed_runs': len(runs), 'stop_reason_counts': dict(Counter(s for r in runs for s in r['stop_reasons'])),
            'all_observed_latency': stats([r['total_agent_latency_ms'] for r in runs]) if runs else None,
            'provider_failures': [r['provider_failures'] for r in runs],
            'usage_completeness': dict(Counter(r['usage_completeness'] for r in runs))}
        summary[variant]['all_observed_metrics'] = {key: stats([r[key] for r in runs]) if runs else None for key in (
            'main_provider_latency_ms', 'auxiliary_provider_latency_ms', 'total_tool_execution_latency_ms',
            'main_provider_requests', 'tool_calls_total', 'parallel_eligible_batch_count', 'actual_parallel_batch_count')}
        summary[variant]['tool_calls_per_response_distribution'] = dict(Counter(n for r in runs for n in r['tool_calls_per_response']))
        summary[variant]['token_usage_per_run'] = [r['provider_usage'] for r in runs]
        summary[variant]['retry_attempts'] = [a for p in pairs if variant in p['runs']
                                             for a in p['runs'][variant].get('attempts', [])[1:]]
    if valid:
        ratios = [p['runs']['OFF']['metrics']['total_agent_latency_ms']/p['runs']['ON']['metrics']['total_agent_latency_ms'] for p in valid]
        summary.update(median_off_over_on_agent_latency=median(ratios),
            mean_observed_agent_latency_reduction_pct=mean((1-1/r)*100 for r in ratios),
            causal_claim='NOT_IDENTIFIED: Agent trajectories may differ; replay same trace to isolate scheduler')
    return summary


def select_attempt(base, *, resume, retry_failed):
    """完整样本先核验；仅显式允许时为失败样本分配新目录。"""
    directories = ([base] if base.exists() else []) + sorted(
        (p for p in base.parent.glob(base.name + '-attempt-*') if p.is_dir()),
        key=lambda p: int(p.name.rsplit('-', 1)[1]))
    history = []
    for directory in directories:
        if not resume:
            raise RuntimeError('RUN_DIRECTORY_EXISTS')
        metric, index_path = directory/'metrics.json', directory/'evidence-index.json'
        if not metric.exists() or not index_path.exists():
            raise RuntimeError(f'INCOMPLETE_RUN_REQUIRES_MANUAL_REVIEW:{directory}')
        index = json.loads(index_path.read_text())
        if any(not (directory/p).is_file() or digest(directory/p) != h for p,h in index['files'].items()):
            raise RuntimeError('SAVED_EVIDENCE_CHANGED')
        history.append({'directory': str(directory), 'metrics': json.loads(metric.read_text()),
                        'evidence_index': str(index_path)})
    if history:
        if not history[-1]['metrics']['stop_reasons'] or not retry_failed:
            return Path(history[-1]['directory']), history
        reasons = set(history[-1]['metrics']['stop_reasons'])
        if 'PROVIDER_FAILURE' not in reasons or reasons - {'PROVIDER_FAILURE', 'AGENT_NOT_COMPLETED'}:
            raise RuntimeError('FAILED_RUN_NOT_RETRYABLE: 仅允许显式重试Provider失败，协议/源码异常需人工处理')
        return base.with_name(base.name + f'-attempt-{len(history)+1}'), history
    return base, history


def counted_history(output):
    """把旧attempt及新计数样本都计入预算，失败不能被替换。"""
    rows = {'ON': [], 'OFF': []}
    directories = [p for pattern in ('pair-*/ON*', 'pair-*/OFF*', 'counted-attempts/*')
                   for p in output.glob(pattern) if p.is_dir()]
    for directory in directories:
        if not (directory/'metrics.json').exists():
            raise RuntimeError(f'INCOMPLETE_RUN_REQUIRES_MANUAL_REVIEW:{directory}')
    files = list(output.glob('pair-*/*/metrics.json')) + list(output.glob('counted-attempts/*/metrics.json'))
    for metric in sorted(files):
        directory = metric.parent
        index_path = directory/'evidence-index.json'
        if not index_path.exists():
            raise RuntimeError(f'INCOMPLETE_RUN_REQUIRES_MANUAL_REVIEW:{directory}')
        index = json.loads(index_path.read_text())
        if any(not (directory/p).is_file() or digest(directory/p) != h for p,h in index['files'].items()):
            raise RuntimeError('SAVED_EVIDENCE_CHANGED')
        metrics = json.loads(metric.read_text())
        variant = metrics['variant']
        rows[variant].append({'directory': str(directory), 'metrics': metrics, 'evidence_index': str(index_path)})
    return rows


def run_counted(output, manifest_path, manifest, budget):
    """每侧总次数固定，已记录Provider/Agent失败也消耗一个名额。"""
    history = counted_history(output)
    if any(len(rows) > budget for rows in history.values()):
        raise RuntimeError('EXISTING_ATTEMPTS_EXCEED_BUDGET')
    report_path = output/f'counted-report-{manifest_path.stem}.json'
    manifest_hash = digest(manifest_path)
    report = {'status': 'RUNNING', 'manifest': str(manifest_path), 'budget_per_variant': budget,
              'count_failures': True, 'attempts': history, 'limitation': 'ordinal slots, not equal model trajectories; historical retries count independently'}

    def persist():
        report['summary'] = {v: {'attempt_count': len(history[v]),
            'success_count': sum(not r['metrics']['stop_reasons'] for r in history[v]),
            'failure_count': sum(bool(r['metrics']['stop_reasons']) for r in history[v]),
            'all_attempt_latency': stats([r['metrics']['total_agent_latency_ms'] for r in history[v]]) if history[v] else None,
            'stop_reason_counts': dict(Counter(s for r in history[v] for s in r['metrics']['stop_reasons']))}
            for v in ('ON', 'OFF')}
        save(report_path, report)

    try:
        persist()
        for slot in range(1, budget+1):
            for variant in (('ON','OFF') if slot%2 else ('OFF','ON')):
                if len(history[variant]) >= slot:
                    continue
                validate(manifest)
                if digest(manifest_path) != manifest_hash:
                    raise RuntimeError('MANIFEST_CHANGED')
                directory = output/'counted-attempts'/f'{variant}-{slot:04d}'
                if directory.exists():
                    raise RuntimeError(f'INCOMPLETE_RUN_REQUIRES_MANUAL_REVIEW:{directory}')
                directory.parent.mkdir(parents=True, exist_ok=True)
                print(f'START counted {variant} {slot}/{budget} (failures included)', flush=True)
                with (directory.parent/f'{directory.name}-worker.log').open('x') as log:
                    proc = subprocess.run([sys.executable, '-m', 'eval.tool_hardening.v3_open_audit',
                        '--manifest', str(manifest_path), '--worker-variant', variant, '--worker-output', str(directory)],
                        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                if proc.returncode or not (directory/'metrics.json').exists():
                    raise RuntimeError(f'WORKER_INFRA_FAILURE:{directory}; incomplete evidence requires review')
                metrics = json.loads((directory/'metrics.json').read_text())
                history[variant].append({'directory': str(directory), 'metrics': metrics,
                    'evidence_index': str(directory/'evidence-index.json')})
                persist()
                fatal = set(metrics['stop_reasons']) - {'PROVIDER_FAILURE', 'AGENT_NOT_COMPLETED'}
                if fatal:
                    raise RuntimeError(';'.join(sorted(fatal)))
                if metrics['stop_reasons']:
                    print(f'RECORDED FAILURE {variant} {slot}/{budget}: {metrics["stop_reasons"]}; continue', flush=True)
        validate(manifest)
        report['status'] = 'BUDGET_COMPLETED_MANUAL_REVIEW_PENDING'
    except BaseException as error:
        report.update(status='STOPPED', stop_reason=f'{type(error).__name__}: {error}')
        persist()
        raise
    persist()
    print(str(report_path), flush=True)


def worker(args):
    """每个样本独立子进程、workspace、Session；不自动重试整个样本。"""
    manifest = json.loads(args.manifest.read_text())
    client = validate(manifest)
    if args.worker_output.exists():
        raise RuntimeError('RUN_DIRECTORY_EXISTS: 禁止覆盖或自动重跑已有样本')
    artifact = run_one({}, args.worker_output, args.worker_variant == 'ON', client, manifest['source_hashes'],
                       instruction=manifest['query'], evidence_oracle=manual_evidence, capture_timing=True,
                       production_profile=manifest.get('tool_execution_profile') == 'production_defaults_auto_approve_v1')
    metrics = project(artifact, args.worker_output)
    metrics['strict_citation_pass'] = None
    metrics['strict_citation_status'] = 'NOT_APPLICABLE'
    metrics['task_status'] = 'PENDING_MANUAL_REVIEW'
    metrics['query'] = manifest['query']
    metrics['eval_manifest'] = str(args.manifest)
    metrics['eval_manifest_sha256'] = digest(args.manifest)
    validate(manifest)
    save(args.worker_output/'metrics.json', metrics)
    save(args.worker_output/'evidence-index.json', {'model_requests': len(artifact['turns']),
        'artifact': str(args.worker_output/'artifact.json'), 'transcript': metrics['transcript'],
        'files': {str(p.relative_to(args.worker_output)): digest(p) for p in sorted(args.worker_output.rglob('*'))
                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'},
        'source_hashes': manifest['source_hashes'], 'model': manifest['model'], 'provider': manifest['provider']})


def main():
    """用户启动多对实验，交替顺序，支持跳过已完整保存的样本。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--preflight', type=Path)
    parser.add_argument('--pairs', type=int, default=5)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--accept-eval-revision', action='store_true')
    parser.add_argument('--count-failures', action='store_true', help='每侧次数含已有失败；失败计数后继续，不追求5次成功')
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--worker-variant', choices=('ON', 'OFF'))
    parser.add_argument('--worker-output', type=Path)
    args = parser.parse_args()
    if args.worker_variant:
        worker(args)
        return
    if args.output_dir is None or args.preflight is None or args.pairs < 2:
        parser.error('需要 --output-dir、--preflight，以及 --pairs >= 2')
    if (args.retry_failed or args.accept_eval_revision) and not args.resume:
        parser.error('--retry-failed / --accept-eval-revision 必须与 --resume 使用')
    if args.count_failures and args.retry_failed:
        parser.error('--count-failures 不与 --retry-failed 混用')
    output = args.output_dir.resolve()
    preflight = json.loads(args.preflight.read_text())['result']
    client = get_client()
    if preflight['status'] != 'PASS' or preflight['model_id'] != get_model():
        raise RuntimeError('PREFLIGHT_NOT_MATCHED')
    manifest_path = output/'manifest.json'
    if args.resume:
        revisions = sorted(output.glob('manifest-revision-*.json'))
        if revisions:
            manifest_path = revisions[-1]
        manifest = json.loads(manifest_path.read_text())
        if manifest['pairs'] != args.pairs or manifest['preflight_sha256'] != digest(args.preflight):
            raise RuntimeError('RESUME_CONTRACT_CHANGED')
        if manifest['eval_hashes'] != eval_hashes() and args.accept_eval_revision:
            revised = deepcopy(manifest)
            revised['previous_manifest'] = str(manifest_path)
            revised['previous_manifest_sha256'] = digest(manifest_path)
            revised['eval_hashes'] = eval_hashes()
            revised['revision_reason'] = 'explicitly accepted eval-only provider observation and retry reporting fix'
            revised['count_failures'] = args.count_failures
            revised['changed_eval_files'] = [p for p in set(manifest['eval_hashes']) | set(revised['eval_hashes'])
                if manifest['eval_hashes'].get(p) != revised['eval_hashes'].get(p)]
            validate(revised)
            manifest_path = output/f'manifest-revision-{len(revisions)+1:04d}.json'
            save(manifest_path, revised)
            manifest = revised
        validate(manifest)
    else:
        output.mkdir(parents=True, exist_ok=False)
        manifest = {'experiment': 'v3-open-tool-design-audit-v1', 'query': QUERY, 'pairs': args.pairs,
            'model': get_model(), 'provider': str(client.base_url), 'source_hashes': source_hashes(), 'eval_hashes': eval_hashes(),
            'preflight': str(args.preflight.resolve()), 'preflight_sha256': digest(args.preflight),
            'tool_execution_profile': 'production_defaults_auto_approve_v1',
            'allowed_tools': 'full production registry; runtime exposure prerequisites unchanged',
            'permission': 'auto_approve; production hard denials preserved',
            'order': 'pair1 ON/OFF; pair2 OFF/ON; alternate', 'task_quality': 'manual review, no JSON output contract',
            'budget': 'HexgentConfig defaults; both variants identical',
            'timing': 'both variants profiled; per-handler and event offsets saved; no causal E2E claim',
            'retries': 'no automatic run retry; SDK/Production existing retry policy unchanged', 'automatic_trace_replay': False,
            'count_failures': args.count_failures}
        save(manifest_path, manifest)
    manifest_hash = digest(manifest_path)
    if args.count_failures:
        run_counted(output, manifest_path, manifest, args.pairs)
        return
    report_path = output/('report-' + manifest_path.stem + '.json' if manifest_path.name != 'manifest.json' else 'report.json')
    report = {'status': 'RUNNING', 'manifest': str(manifest_path), 'manifest_sha256': manifest_hash, 'pairs': [
        {'pair': i+1, 'order': ['ON','OFF'] if i%2 == 0 else ['OFF','ON'], 'runs': {}} for i in range(args.pairs)]}
    try:
        for pair in report['pairs']:
            for variant in pair['order']:
                base = output/f'pair-{pair["pair"]}'/variant
                directory, history = select_attempt(base, resume=args.resume, retry_failed=args.retry_failed)
                metric = directory/'metrics.json'
                validate(manifest)
                if digest(manifest_path) != manifest_hash:
                    raise RuntimeError('MANIFEST_CHANGED')
                if directory.exists():
                    if not args.resume or not metric.exists() or not (directory/'evidence-index.json').exists():
                        raise RuntimeError(f'INCOMPLETE_RUN_REQUIRES_MANUAL_REVIEW:{directory}')
                    index = json.loads((directory/'evidence-index.json').read_text())
                    if any(not (directory/p).is_file() or digest(directory/p) != h for p,h in index['files'].items()):
                        raise RuntimeError('SAVED_EVIDENCE_CHANGED')
                else:
                    directory.parent.mkdir(parents=True, exist_ok=True)
                    print(f'START pair={pair["pair"]} {variant}', flush=True)
                    with (directory.parent/f'{directory.name}-worker.log').open('x') as log:
                        proc = subprocess.run([sys.executable, '-m', 'eval.tool_hardening.v3_open_audit',
                            '--manifest', str(manifest_path), '--worker-variant', variant, '--worker-output', str(directory)],
                            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                    if proc.returncode or not metric.exists():
                        raise RuntimeError(f'WORKER_FAILURE:{directory}; see {variant}-worker.log')
                metrics = json.loads(metric.read_text())
                if not history or history[-1]['directory'] != str(directory):
                    history.append({'directory': str(directory), 'metrics': metrics, 'evidence_index': str(directory/'evidence-index.json')})
                pair['runs'][variant] = {'metrics': metrics, 'directory': str(directory), 'attempts': history,
                                        'evidence_index': str(directory/'evidence-index.json')}
                report['summary'] = summarize(report['pairs'])
                save(report_path, report)
                if metrics['stop_reasons']:
                    raise RuntimeError(';'.join(metrics['stop_reasons']))
            left,right = (deepcopy(pair['runs'][v]['metrics']['execution_config']) for v in ('ON','OFF'))
            left['tool_parallel'].pop('enabled');right['tool_parallel'].pop('enabled')
            if left != right:
                raise RuntimeError('NON_SCHEDULER_CONFIG_DIFFERENCE')
            artifacts = {v: json.loads((Path(pair['runs'][v]['directory'])/'artifact.json').read_text()) for v in ('ON','OFF')}
            pair['first_captured_request_equal'] = artifacts['ON']['turns'][0]['request'] == artifacts['OFF']['turns'][0]['request']
            pair['semantic_tool_trajectory_equal'] = trajectory(artifacts['ON'],Path(pair['runs']['ON']['directory'])) == trajectory(artifacts['OFF'],Path(pair['runs']['OFF']['directory']))
        report['status'] = 'COMPLETED_MANUAL_REVIEW_PENDING'
        validate(manifest)
    except BaseException as error:
        report.update(status='STOPPED', stop_reason=f'{type(error).__name__}: {error}')
        report['summary'] = summarize(report['pairs'])
        save(report_path, report)
        raise
    report['summary'] = summarize(report['pairs'])
    save(report_path, report)
    print(str(report_path), flush=True)


if __name__ == '__main__':
    main()
