"""B1-B A/B/C/D 真实环境验收，不执行 Agent 或 Benchmark 业务任务。"""

import argparse
import json
from pathlib import Path

from dotenv import dotenv_values
import httpx

from .environment import EnvironmentError, LocalDocker, TrialEnvironmentManager, dump


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--build-metadata', type=Path, required=True)
    parser.add_argument('--workspace-source', type=Path, required=True)
    parser.add_argument('--docker-socket', required=True)
    parser.add_argument('--embedding-env-file', type=Path)
    args=parser.parse_args()
    metadata=json.loads(args.build_metadata.read_text())
    key=dotenv_values(args.embedding_env_file).get('DASHSCOPE_API_KEY', '') if args.embedding_env_file else ''
    manager=TrialEnvironmentManager(args.root, args.workspace_source, metadata['workspace']['image_id'],
        metadata['runtime']['image_id'], LocalDocker(args.docker_socket), embedding_key=key or '')
    report=dict(schema_version=1, stage='B1-B', status='NOT RUN', cases={}, build=metadata,
                agent_runs=0, embedding_enabled=bool(key))

    def provision(identifier):
        e=manager.provision(identifier, 'b1b-seed-v1')
        dump(e.artifact_root/'build-metadata.json',metadata)
        result=manager.readiness(e, require_seed=False, require_vector=False, timeout_seconds=120)
        if result['status']!='PASS': raise EnvironmentError('基础服务未就绪')
        return e

    def ready(e):
        manager.seed(e, 'b1b-seed-v1')
        result=manager.readiness(e, timeout_seconds=180)
        if result['status']!='PASS': raise EnvironmentError('业务文档／解析／向量未就绪')
        return manager.observe(e)

    try:
        # A：完整建立、业务就绪、归档与销毁。
        first=provision('rebuild-001'); initial=ready(first)
        manager.archive(first); manager.destroy(first)
        report['cases']['A']={'status':'PASS', 'semantic_sha256': initial['semantic_sha256'],
                              'artifact_root': str(first.artifact_root)}
        dump(args.root/'verification.json', report)

        # B：相同基线的两个完整部署，修改 A 后独立读取 B，并在销毁 A 后继续读取 B。
        a=provision('isolation-a'); a_before=ready(a)
        b=provision('isolation-b'); b_before=ready(b)
        if a_before['semantic_sha256']!=b_before['semantic_sha256']: raise AssertionError('初始状态不等价')
        cross=httpx.get(b.endpoints['workspace']+'/api/v1/users/me',
            headers={'Authorization':'Bearer '+manager._credentials(a)['USER_TOKEN']}, timeout=5, trust_env=False)
        if cross.status_code!=401: raise AssertionError('跨 Trial JWT 未拒绝')
        seed=json.loads((a.artifact_root/'seed.json').read_text())
        document=a_before['documents'][0]
        manager._api(a, 'PUT', f"/api/v1/workspaces/{seed['workspace_id']}/documents/{document['document_code']}/content",
            {'expectedVersionId':document['version_id'], 'content':'# 隔离验证\n仅 Trial A 可见：ISOLATION-MAPLE\n'})
        a_after=manager.observe(a); b_after=manager.observe(b)
        if a_after['semantic_sha256']==a_before['semantic_sha256']: raise AssertionError('A 实际状态没有修改')
        if b_after['semantic_sha256']!=b_before['semantic_sha256']: raise AssertionError('B 被污染')
        dump(a.artifact_root/'isolation-observations.json', {'before':a_before,'after':a_after})
        dump(b.artifact_root/'isolation-observations.json', {'before':b_before,'after_a_mutation':b_after})

        # D：创建真实 Runtime Session，但不 StartRun；检查实际挂载路径及本地状态路径。
        session_ids=[]
        for e in [a,b]:
            response=httpx.post(e.endpoints['runtime']+'/api/sessions', json={'permission_mode':'dont_ask'},
                                timeout=15, trust_env=False)
            response.raise_for_status(); session_id=response.json()['session_id']; session_ids.append(session_id)
            memory=e.workdir/'.hexgent/memory'; memory.mkdir(parents=True, exist_ok=True)
            (memory/'isolation-marker.txt').write_text(e.trial_id)
            checkpoints=e.workdir/'.hexgent/sessions'/session_id/'checkpoints'
            checkpoints.mkdir(parents=True, exist_ok=True)
            (checkpoints/'isolation-marker.json').write_text(json.dumps({'trial_id':e.trial_id}))
            container=json.loads(manager.docker.run(['container','inspect',manager._container(e,'runtime')]))[0]
            mount=next(m for m in container['Mounts'] if m['Destination']=='/workdir')
            if Path(mount['Source']).resolve()!=e.workdir.resolve(): raise AssertionError('Runtime workdir 挂载不匹配')
            transcript=e.workdir/'.hexgent/sessions'/session_id/'transcript.jsonl'
            if not transcript.is_file(): raise AssertionError('真实 Session Transcript 未生成')
        if a.workdir==b.workdir or session_ids[0]==session_ids[1]: raise AssertionError('本地状态路径未隔离')
        if (b.workdir/'.hexgent/memory/isolation-marker.txt').read_text()!='isolation-b': raise AssertionError('Memory 路径污染')
        report['cases']['D']={'status':'PASS', 'agent_execution':'NOT RUN',
            'session_ids':session_ids, 'workdirs':[str(a.workdir),str(b.workdir)],
            'checkpoint_note':'仅验证路径；marker 不是生产 Checkpoint 的恢复证据'}

        manager.archive(a); manager.destroy(a)
        b_survives=manager.observe(b)
        if b_survives['semantic_sha256']!=b_before['semantic_sha256']: raise AssertionError('销毁 A 影响 B')
        seed_b=json.loads((b.artifact_root/'seed.json').read_text())
        detail=manager._api(b,'GET',f"/api/v1/workspaces/{seed_b['workspace_id']}/documents/{b_before['documents'][0]['document_code']}")
        if not detail.get('content'): raise AssertionError('B 文档无法继续读取')
        report['cases']['B']={'status':'PASS','initial_equivalent':True,'a_mutated':True,
                              'b_unchanged':True,'b_readable_after_a_destroy':True,'cross_trial_jwt_rejected':True}

        # C：故意省略 Seed，真实依赖上的业务 Readiness 必须 INVALID，之后安全清理。
        failed=provision('failed-seed')
        result=manager.readiness(failed,timeout_seconds=3,poll_seconds=1)
        if result['status']!='INVALID': raise AssertionError('未 Seed 环境被误判 Ready')
        manager.archive(failed); manager.destroy(failed); manager.destroy(failed)
        if manager.observe(b)['semantic_sha256']!=b_before['semantic_sha256']: raise AssertionError('失败清理影响 B')
        report['cases']['C']={'status':'PASS','readiness':'INVALID','failure':'SEED_INTENTIONALLY_OMITTED',
                              'cleanup':'PASS','other_trial_unchanged':True}
        manager.archive(b); manager.destroy(b)
        report['status']='PASS'
    except EnvironmentError as exc:
        report.update(status='BLOCKED',reason=str(exc))
    except Exception as exc:
        report.update(status='FAIL',reason=type(exc).__name__)
    finally:
        cleanup=[]
        for environment in manager.environments.values():
            if environment.destroyed: continue
            try:
                manager.archive(environment); manager.destroy(environment)
                cleanup.append({'trial_id':environment.trial_id,'status':'PASS'})
            except Exception as exc:
                cleanup.append({'trial_id':environment.trial_id,'status':'FAIL','error':type(exc).__name__})
        report['final_cleanup']=cleanup
        if any(c['status']=='FAIL' for c in cleanup): report['status']='FAIL'
        dump(args.root/'verification.json',report)
        print(json.dumps({'status':report['status'],'cases':report['cases'],'reason':report.get('reason'),
                          'artifact':str(args.root/'verification.json')},ensure_ascii=False))


if __name__=='__main__':
    main()
