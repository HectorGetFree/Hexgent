"""R-L3 E2E 前置核查：只读源码与固定请求，不实例化 Provider。"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from hexgent.context.context_budget import ContextLimits
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION
from hexgent.utils.token_estimator import estimate_request_tokens
from .historical_prompts import V2

ROOT = Path(__file__).resolve().parents[2]

def main():
    if EXTRACTION_PROMPT_VERSION == 'key-fact-extraction-v2':
        raise RuntimeError('此入口仅复现历史v1接线核查；Production已默认v2，请使用integration_closure离线验收。')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    out=ROOT/'eval/context_remediation_v1/runs'/('rl3-e2e-precheck-'+stamp)
    out.mkdir(parents=True,exist_ok=False)
    def save(name,value):
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    limits=ContextLimits()
    source=ROOT/'eval/context_remediation_v1/runs/preflight-review-20261012-v2/candidate-requests/R-L3.json'
    requests=json.loads(source.read_text())
    extractor=estimate_request_tokens(V2,requests['extraction']['input'],[])
    summary=estimate_request_tokens(requests['summary']['instructions'],requests['summary']['input'],[])
    before={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hexgent').rglob('*.py')}
    fixture=ROOT/'eval/context_management/c2_prep_2/long_fixtures/L3.json'
    oracle=ROOT/'eval/context_management/c2_prep_3/oracle_v2/L3.json'
    save('manifest.json',dict(created_at=datetime.now(timezone.utc).isoformat(),stage='PRE_EXECUTION_ONLY',fixture='R-L3',
        production_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),production_hashes=before,
        fixture_sha256=sha256(fixture.read_bytes()).hexdigest(),oracle_sha256=sha256(oracle.read_bytes()).hexdigest(),
        production_extractor_prompt_version=EXTRACTION_PROMPT_VERSION,v2_request_instructions_sha256=sha256(V2.encode()).hexdigest(),
        capacity='USER_ASSERTED_CAPACITY_128K_UNVERIFIED',provider_calls=0,network_calls=0,full_e2e_attempts_started=0))
    save('budget.json',dict(estimator='Production estimate_request_tokens',provider_usage=None,monetary_cost='NOT_AVAILABLE_PROVIDER_TARIFF_NOT_VERIFIED',
        minimum_calls=4,assumptions=['Extractor一次','Summary一次，不进行候选重试','两个MAIN各一次模型响应，无额外工具轮次'],
        extractor_input=extractor,extractor_output_limit=limits.fact_extraction_output_tokens,
        summary_input=summary,summary_output_limit=limits.summary_output_tokens,
        main_each_conservative_capacity_ceiling=limits.context_window_tokens-limits.safety_margin_tokens,
        four_call_conservative_ceiling=extractor+limits.fact_extraction_output_tokens+summary+limits.summary_output_tokens+2*(limits.context_window_tokens-limits.safety_margin_tokens),
        actual_main_request_estimates='NOT_ASSEMBLED',hard_total_call_limit='NOT_FROZEN',
        risks=['MAIN自主工具调用可能增加轮次，此数不是任意Agent轨迹的总上限','当前Production提取超时120秒，不能把旧Eval300秒视为Production配置','Provider容量错误属于INFRA','未执行连通性或容量探测']))
    result=dict(gate='C2_RL3_E2E_INCOMPLETE',engineering='NOT_EXECUTED',task_quality='NOT_EVALUABLE',cost='ESTIMATED_ONLY',provider_calls=0,
        blockers=[dict(code='EXTRACTOR_V2_NOT_PRODUCTION_WIRED',evidence='hexgent/context/key_fact_extractor.py:12,55',
            detail='Production固定v1；v2仅为Eval factorial_pilot请求instructions加PROMPT_ADDITION。直接运行不满足本轮指定v2。'),
            dict(code='E2E_REQUEST_PROFILE_NOT_FROZEN',detail='需要选择并审计Eval-only v2请求适配，或另行批准Production接线；同时冻结MAIN总轮次/调用上限及正式Continue快照方案。')],
        no_fallback_to_v1=True,no_fake_main_substitution=True,no_new_session_resume_substitution=True)
    save('result.json',result)
    diff=subprocess.run(['git','diff','--check'],cwd=ROOT,capture_output=True,text=True)
    after={str(p.relative_to(ROOT)):sha256(p.read_bytes()).hexdigest() for p in (ROOT/'hexgent').rglob('*.py')}
    save('integrity.json',dict(production_unchanged=before==after,fixture_unchanged=sha256(fixture.read_bytes()).hexdigest()==json.loads((out/'manifest.json').read_text())['fixture_sha256'],
        git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr)))
    (out/'REPORT.md').write_text('''# C2 R-L3 E2E 执行前核查

Gate：C2_RL3_E2E_INCOMPLETE。未启动完整E2E尝试，零付费模型调用。

当前Production Extractor固定v1，v2只有Eval请求版本。未经说明不能把v1执行标记v2，也不能静默覆盖Production常量或改变Prompt文件。
建议明确采用Eval-only请求适配：仅将Extractor instructions替换为已冻结v2，其余Production提取校验、原子提交、Builder、预算及正式Continue不变；实际请求和版本必须双重记录。这属于实验请求配置，不应声称Production原生已支持v2选择。

最少4次调用，条件为两个MAIN各一轮且无额外调用。估算Extractor输入54650、输出上限4000；Summary输入49361、输出上限2000；两个MAIN按每次120000容量预算计算，合计350011。MAIN完整请求尚未组装，因此这是准备阶段保守容量上界，不是精确请求估算或Provider Usage。
若允许自主工具多轮，需要额外冻结总调用硬上限；不允许无界继续或隐藏重试。价格未核对，不提供货币成本。128K仍仅为用户声明，未探测。

工程、任务质量均NOT_EXECUTED/NOT_EVALUABLE；成本仅本地估算。Production与原Fixture未改，历史Artifact保留。等待请求适配边界与总调用预算确认后，才开始唯一E2E尝试。
''')
    print(json.dumps(dict(output=str(out),**result),ensure_ascii=False))

if __name__=='__main__':main()
