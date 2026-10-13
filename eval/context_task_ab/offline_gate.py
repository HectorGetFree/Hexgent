"""P0可复现离线验收与资料审计；不提供真实模型开关。"""
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import io
import json
import subprocess
import sys
from pathlib import Path
import unittest

from . import test_gate
from .harness import FIXTURE, manifest, config
from .task import CONTRACT
from hexgent.utils.token_estimator import estimate_value_tokens

ROOT=Path(__file__).resolve().parents[2]

def save(path,data):
    with path.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path);p.add_argument('--regression',action='store_true')
    args=p.parse_args();target=args.output_dir or Path(__file__).parent/'runs'/('p0-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    target.mkdir(parents=True,exist_ok=False)
    before=manifest(ROOT/'hexgent');sources=[]
    for path in sorted(FIXTURE.rglob('*.md')):
        text=path.read_text();sources.append(dict(path=str(path.relative_to(FIXTURE)),bytes=path.stat().st_size,
            characters=len(text),estimated_source_tokens=estimate_value_tokens(text),
            role='采购基线' if 'buyer' in path.parts else '供应商专项合同' if 'vendors' in path.parts else '导航'))
    suite=unittest.defaultTestLoader.loadTestsFromModule(test_gate)
    stream=io.StringIO();test_gate.EVIDENCE.clear()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    regressions=[]
    if args.regression:
        commands=[('runtime',['discover','-s','tests','-v']),('legacy_eval',[
            'eval.modules_v1.test_benchmark','eval.memory_m3.test_real_smoke',
            'eval.pinned_constraints.test_gate','eval.pinned_constraints.test_real_smoke',
            'eval.pinned_constraints.test_production_smoke_audit','-v'])]
        for label,arguments in commands:
            proc=subprocess.run([sys.executable,'-m','unittest',*arguments],cwd=ROOT,capture_output=True,text=True,timeout=120)
            (target/(label+'.txt')).write_text(proc.stdout+proc.stderr)
            regressions.append(dict(suite=label,exit_code=proc.returncode,log=label+'.txt',
                status='PASS' if proc.returncode==0 else 'FAIL',not_reclassified_as_pass=True))
    save(target/'regression.json',regressions)
    after=manifest(ROOT/'hexgent');production_unchanged=before==after
    estimated=sum(s['estimated_source_tokens'] for s in sources)
    frozen=dict(task=CONTRACT,fixture_hashes=manifest(FIXTURE),production_hashes=before,
        eval_hashes={k:v for k,v in manifest(Path(__file__).parent).items() if not k.startswith('runs/')},
        config=asdict(config(FIXTURE)),source_inventory=sources,source_tokens_estimate=estimated,
        official_threshold=64000,capacity_status='USER_ASSERTED_CAPACITY_128K_UNVERIFIED',
        real_provider_calls=0,source_coverage={
            'commercial':'许可、实施、连接器；aster旧报价',
            'capacity':'年度配额、超额费率、幂等计量；aster旧费率',
            'identity-and-support':'OIDC和强制年度模块',
            'security':'备份驻留、审计保留、可选BYOK',
            'delivery':'完整交付与部分演示区别'},
        private_oracle_location='eval/context_task_ab/private_oracle/oracle.py (不进入Fixture)')
    save(target/'manifest.json',frozen);save(target/'evidence.json',test_gate.EVIDENCE)
    verdict=dict(gate='P0_OFFLINE_GATE_PASS' if result.wasSuccessful() and production_unchanged else 'P0_OFFLINE_GATE_FAIL',
        tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),
        production_unchanged=production_unchanged,real_provider_calls=0,
        task_success='NOT_EVALUATED',natural_compaction_opportunity='NOT_VERIFIED',
        workload_scale='BELOW_DESIGN_CANDIDATE_RANGE' if estimated<30000 else 'CANDIDATE_RANGE',
        real_model_readiness='REQUIRES_REVIEW_NOT_AUTHORIZED',regression=regressions)
    save(target/'result.json',verdict)
    (target/'tests.txt').write_text(stream.getvalue())
    report=f'''# IS1 P0离线Gate

结论：{verdict['gate']}。{result.testsRun}组离线测试，{len(result.failures)}失败，{len(result.errors)}错误；真实Provider调用未发生。

## 资料与任务
共{len(sources)}份Markdown，{sum(s['bytes'] for s in sources)}字节，正文估算{estimated} Tokens（不是Provider Usage）。8份采购合同、四家各5份专项资料及1份导航。预算、年度请求、报价修订、必需附加费、驻留及交付条件分散在独立来源中，没有推荐答案表。
公开交付合同见buyer/deliverables.md；任务输入见manifest.json。外部Oracle独立计算四家两年费用及资格，逐项验证当前条款引文，不向Agent提供答案。

## 已验证
- 只读资料克隆与重置；ON/OFF快照字节哈希相等。
- 正确输出与错误费用、类型、引文、原资料修改的Oracle反例。
- 真实Production Tool Pipeline拒绝越界、输入写入、Runtime工件访问和bash，拒绝前不dispatch。
- 空Session经真实read_file形成工具批次Checkpoint；自动压缩前截取。
- 新Engine正式Restore→Continue，未追加新UserQuery；不触发压缩时首请求完全一致。
- 两个独立进程从相同Transcript出发；ON调用Production默认v2 Extractor和Summary，OFF不调用辅助模型；均正式Continue。
- OFF硬预算超限零Provider派发；前置尝试额度拒绝额外调用。

## 不可据此声称
Fake工具序列只检查链路，不证明真实模型自主探索、任务成功率、语义提取质量或净Token收益。跨进程压缩探针使用10K缩小阈值，不是正式64K实验。固定Fake Snapshot及摘要不是模型质量证据。
本资料规模低于原设计3–7万源正文估算Tokens候选范围；采购比较信息已具备，但不足以承诺自然轨迹达到64K压缩条件。不得靠重复读取、填充、强制全读或降低正式阈值补救；未来无压缩机会必须报告NO_COMPACTION_OPPORTUNITY。P0通过不等于长上下文任务qualified。
Markdown Oracle仅检查结构、推荐标识，叙述无依据结论仍需独立人工审阅。文件权限不是OS sandbox。OFF替换是Eval进程局部行为，不是Production配置开关。

## 证据
manifest.json包含配置、源码与Fixture哈希；evidence.json包含请求、事件、Transcript和恢复状态；tests.txt保存本轮测试输出。Production前后哈希一致：{production_unchanged}。

## 下一步
仅提交人工审核。真实模型尚未授权；需先确认任务资料规模与自然压缩机会的资格处理，不直接启动A/B。
回归日志与原始退出码另见regression.json；既有失败不计PASS，不擅自修复冻结Fixture。
'''
    (target/'REPORT.md').write_text(report)
    print(json.dumps(dict(output=str(target.resolve()),**verdict),ensure_ascii=False))
    if not result.wasSuccessful() or not production_unchanged:raise SystemExit(1)

if __name__=='__main__':main()
