"""确定性长历史：真实源码正文与明确标注的合成交互，不冒充真实执行轨迹。"""
from pathlib import Path
from hashlib import sha256
from collections import Counter
from eval.context_management.c1_independent import ROOT as REPO, plain, response, save
from hexgent.utils.token_estimator import estimate_value_tokens

ROOT = Path(__file__).resolve().parent
TOPICS = [
    ('L1', '多轮 Runtime 配置与调用链审查', ['hexgent', 'tests'],
     [('当前演练配置监听端口=19347', 'SUMMARY_REQUIRED'), ('早期演练端口=19000', 'OBSOLETE'),
      ('当前快照编号=CONFIG-ELM-2026-R8', 'SUMMARY_REQUIRED'), ('批处理失败后仅重试未提交项', 'SUMMARY_REQUIRED'),
      ('提交前需要核对 Lease 归属与输入修订号', 'SUMMARY_REQUIRED'), ('旧重试策略=全部项无条件重试', 'OBSOLETE'),
      ('本轮最终决策=完成只读审查，发布仍待批准', 'SUMMARY_REQUIRED'), ('禁止改写输入源码快照', 'EXTERNALLY_PROTECTED')]),
    ('L2', '大型工具输出与测试证据阅读', ['tests', 'hexgent'],
     [('目标查询=QUERY-WILLOW-804', 'SUMMARY_REQUIRED'), ('旧残缺响应排名=第2名', 'OBSOLETE'),
      ('目标文档=DOC-W804-6', 'SUMMARY_REQUIRED'), ('完整响应 Dense Top30 未命中', 'SUMMARY_REQUIRED'),
      ('完整响应 Sparse 第7名且 RRF 第11名', 'SUMMARY_REQUIRED'), ('旧结论=目标已进入Top5', 'OBSOLETE'),
      ('最终完整排序=第8名，仍在Top5外', 'SUMMARY_REQUIRED'), ('不得修改查询与标注文件', 'EXTERNALLY_PROTECTED')]),
    ('L3', '长审查任务部分完成及失败恢复交接', ['hexgent', 'tests'],
     [('交接批次=AUDIT-MAPLE-57', 'SUMMARY_REQUIRED'), ('旧进度=全部审查完成', 'OBSOLETE'),
      ('参数与协议审查已完成', 'SUMMARY_REQUIRED'), ('失败恢复后的检索审查已完成', 'SUMMARY_REQUIRED'),
      ('当前阻塞=等待维护者确认两处权限契约', 'SUMMARY_REQUIRED'), ('旧建议=跳过权限审查直接发布', 'OBSOLETE'),
      ('下一步=确认权限契约后生成交接报告，不执行发布', 'SUMMARY_REQUIRED'), ('禁止执行实际服务发布', 'EXTERNALLY_PROTECTED')]),
]


def freeze(path, value):
    import json
    body = json.dumps(plain(value), ensure_ascii=False, indent=2)
    if path.exists() and path.read_text() != body:
        raise RuntimeError(f'拒绝覆盖冻结长样本：{path}')
    if not path.exists():
        save(path, value)


def build():
    manifest = []
    for identifier, title, dirs, specs in TOPICS:
        files = [p for directory in dirs for p in sorted((REPO/directory).rglob('*.py'))]
        # 1. 每份正文只读一次；真实文件内容提供规模，不重复填充同一段。
        rounds, source_hashes = [], {}
        for index, path in enumerate(files):
            content = path.read_text()
            relative = str(path.relative_to(REPO))
            source_hashes[relative] = sha256(content.encode()).hexdigest()
            call_id = f'{identifier}:read:{index}'
            query = f'继续只读审查 {relative}，区分实现、测试断言与尚未验证的行为。'
            intro = f'模拟工具返回：文件 {relative} 的冻结源码正文如下。它是阅读证据，不是本轮执行测试结果。\n'
            if identifier == 'L3':
                intro += f'工作项 {index+1}：完成正文阅读；依赖上一批接口梳理。断言存在不等于实际通过，失败恢复与权限结论需要独立证据。\n'
            elif identifier == 'L2':
                intro += '保留各测试的输入、异常、预期状态和 Call ID 断言；不要把不同测试的事实拼接成一次执行结果。\n'
            else:
                intro += '记录参数默认值、入口调用方、状态转换及边界检查。下方是 Hexgent 代码参考，不是合成演练部署的配置权威。\n'
            call = dict(type='function_call', call_id=call_id, name='read_file', arguments='{"path":'+ __import__('json').dumps(relative)+'}')
            output = dict(type='function_call_output', call_id=call_id, output=intro+content)
            rounds.append(dict(query=query, messages=[call, output]+plain(response('已阅读该工作项；保留其来源，继续追踪依赖和边界。').output)))
            history = [m for r in rounds for m in [{'role':'user','content':r['query']}]+r['messages']]
            if estimate_value_tokens(history) >= 72000:
                break
        if estimate_value_tokens(history) < 72000:
            raise RuntimeError(f'{identifier} 实际唯一源码不足以达到默认压力规模')
        # 2. Ground Truth 在原始来源中分布，不向摘要 Provider 提供独立 Oracle。
        positions = [0, 1, len(rounds)//4, len(rounds)//2, len(rounds)*3//5,
                     len(rounds)//3, len(rounds)*13//20, len(rounds)-1]
        facts = []
        for n, ((text, channel), pos) in enumerate(zip(specs, positions)):
            result = rounds[pos]['messages'][1]
            result['output'] += '\n合成审查台账证据：' + ('该项已废止，不再生效：' if channel=='OBSOLETE' else '当前有效：') + text
            facts.append(dict(fact_id=f'{identifier}:fact:{n+1}', source_round=pos,
                source_call_id=result['call_id'], quote=text, canonical_fact=text,
                importance='HISTORICAL' if channel=='OBSOLETE' else 'CRITICAL',
                status='OBSOLETE' if channel=='OBSOLETE' else 'ACTIVE', channel=channel,
                rule='实体、取值、条件和生效状态一致，允许释义；历史提及不等于激活。'))
        history = [m for r in rounds for m in [{'role':'user','content':r['query']}]+r['messages']]
        fixture = dict(id=identifier, title=title, evidence_type='SYNTHETIC_LONG_HISTORY',
            rounds=rounds, source_hashes=source_hashes, final_query='继续当前交接，使用当前有效结论，不进行业务变更。')
        fp, op = ROOT/'long_fixtures'/f'{identifier}.json', ROOT/'long_oracles'/f'{identifier}.json'
        freeze(fp, fixture)
        freeze(op, dict(fixture_id=identifier, facts=facts, review_status='MANUAL_REVIEW_REQUIRED'))
        outputs = [r['messages'][1]['output'] for r in rounds]
        manifest.append(dict(id=identifier, title=title, fixture_hash=sha256(fp.read_bytes()).hexdigest(),
            oracle_hash=sha256(op.read_bytes()).hexdigest(), rounds=len(rounds), tool_calls=len(rounds),
            messages=len(history), unique_source_files=len(source_hashes), source_bytes=sum((REPO/p).stat().st_size for p in source_hashes),
            character_count=len(str(history)), history_estimated_tokens=estimate_value_tokens(history),
            tool_output_char_ratio=sum(len(o) for o in outputs)/sum(len(str(m)) for m in history),
            fact_counts=dict(Counter(f['channel'] for f in facts)), production_context_threshold=64000,
            classification='C2-B', evidence_type='SYNTHETIC_LONG_HISTORY'))
    freeze(ROOT/'long_fixture_manifest.json', manifest)
    return manifest
