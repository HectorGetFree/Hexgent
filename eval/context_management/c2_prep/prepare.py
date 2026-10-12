"""C2 准备：冻结合成历史与独立标注，仅执行离线 Production Dry Run。"""
import hashlib
import json
from pathlib import Path
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timezone

from .catalog import CASES
from eval.context_management.c1_independent import Gate, Recorder, hashes, plain, response, save
from eval.tool_hardening.v3_provider import Observer
from hexgent.agent_engine import AgentEngine
from hexgent.config import validate_config
from hexgent.context.context_builder import build_context_details
from hexgent.session.pinned_constraints import active_constraints
from hexgent.loop.agent_turn import _available_tools_for_state
from hexgent.loop.loop_state import LoopState
from hexgent.utils.token_estimator import estimate_request_tokens

ROOT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(path, value):
    body = json.dumps(plain(value), ensure_ascii=False, indent=2)
    if path.exists() and path.read_text() != body:
        raise RuntimeError(f"拒绝覆盖冻结资产：{path}")
    if not path.exists():
        save(path, value)


def assets(case):
    messages, facts = [], []
    for index, report in enumerate(case['reports']):
        selected = [(n, f) for n, f in enumerate(case['facts'])
                    if (0 if f[2] == 'OBSOLETE' else 1 + n % 2) == index]
        quote_lines = ['历史记录，当前已废止：' + f[1] if f[2] == 'OBSOLETE'
                       else '当前核对结论：' + f[1] for _, f in selected]
        text = report + '\n' + '\n'.join(quote_lines)
        msg_id = f"{case['id']}:source:{index}"
        messages.append(dict(id=msg_id, role='user' if case['family'] == 'F2' and index else 'tool',
                             text=text, source='SYNTHETIC_FIXED_HISTORY'))
        for n, (subject, assertion, channel) in selected:
            facts.append(dict(fact_id=f"{case['id']}:fact:{n+1}", subject=subject,
                source_message_id=msg_id, quote=assertion, canonical_fact=assertion,
                importance='HISTORICAL' if channel == 'OBSOLETE' else 'CRITICAL',
                status='OBSOLETE' if channel == 'OBSOLETE' else 'ACTIVE', channel=channel,
                rule='保留实体、取值、单位和条件；允许同义表达。旧事实仅历史提及不算激活。'))
    return dict(id=case['id'], family=case['family'], title=case['title'], messages=messages), facts


def quality(facts, judgments, claims=None):
    """独立人工判定的聚合器，不用字符串匹配冒充语义 Judge。"""
    allowed = {'PRESENT_CORRECT', 'MISSING', 'INCORRECT', 'UNCERTAIN'}
    required = [f for f in facts if f['channel'] == 'SUMMARY_REQUIRED']
    if any(judgments.get(f['fact_id']) not in allowed for f in required):
        raise ValueError('必要事实的人工判定缺失或非法')
    correct = sum(judgments[f['fact_id']] == 'PRESENT_CORRECT' for f in required)
    uncertain = sum(judgments[f['fact_id']] == 'UNCERTAIN' for f in required)
    return dict(critical_fact_recall=correct / len(required),
                uncertainty_upper_bound=(correct + uncertain) / len(required),
                unsupported_claim_rate=None if claims is None or not claims else
                sum(c == 'UNSUPPORTED' for c in claims) / len(claims))


class OfflineObserver(Observer):
    def response(self, row, value):
        value = plain(value)
        row.update(response=value, response_id=value.get('id'), usage=value.get('usage'), tool_calls=[])


class StreamRecorder(Recorder):
    def create(self, **request):
        yield from super().create(**request)


def main():
    # 1. 在执行前冻结全部源数据及独立 Oracle，拒绝反向改写。
    before = hashes()
    entries = []
    for case in CASES:
        fixture, facts = assets(case)
        fp, op = ROOT / 'fixtures' / (case['id'] + '.json'), ROOT / 'oracles' / (case['id'] + '.json')
        freeze(fp, fixture)
        freeze(op, dict(fixture_id=case['id'], facts=facts, review_status='MANUAL_REVIEW_REQUIRED'))
        entries.append(dict(id=case['id'], family=case['family'], fixture_sha256=digest(fp), oracle_sha256=digest(op)))
    freeze(ROOT / 'manifest.json', dict(schema_version=1, fixtures=entries, production_hashes=before,
        evidence_type='SYNTHETIC_FIXED_HISTORY', paid_calls=0))
    output = ROOT / ('dry_run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    if output.exists():
        raise RuntimeError('Dry Run 目录已存在，不覆盖历史结果')
    output.mkdir()
    results = []
    for case in CASES:
        directory = output / case['id']
        directory.mkdir()
        fixture, facts = assets(case)
        gate = Gate(output)
        engine = AgentEngine(gate.config(), directory / 'workspace')
        evidence = {'requests': []}
        try:
            # 2. 固定历史包含完整的调用/结果轮次；历史 Handler 不在本轮重新派发。
            for index, msg in enumerate(fixture['messages']):
                query = engine.context_manager.append_user_query(msg['text'] if msg['role'] == 'user' else '核对资料来源 ' + msg['id'])
                engine.session.record_user_query(query)
                if msg['role'] == 'tool':
                    call = dict(type='function_call', call_id=msg['id'], name='read_file', arguments=json.dumps({'path': f'source-{index}.md'}))
                    result = dict(type='function_call_output', call_id=msg['id'], output=msg['text'])
                    messages = [call, result] + response('资料已核对，尚未执行业务操作。').output
                else:
                    messages = response('已收到本轮明确更正。').output
                engine.context_manager.append_messages(messages, completes_round=True)
                engine.session.record_context_messages(messages, completes_round=True, source='synthetic_fixture')
            for fact in facts:
                if fact['channel'] == 'EXTERNALLY_PROTECTED':
                    engine.manage_pinned_constraint('pin', fact['fact_id'], text=fact['quote'])
            pins = active_constraints(engine.session.store)
            instructions = build_context_details(engine.tool_context, pinned_constraints=pins).instructions
            tools = _available_tools_for_state(LoopState(engine.context_manager, engine.tool_context))
            latest = '继续核对当前有效事实与待办状态，不执行业务变更。'
            base = estimate_request_tokens(instructions, [{'role': 'user', 'content': latest}], tools)
            engine.config = validate_config(replace(engine.config, context=replace(engine.config.context,
                proactive_threshold_tokens=base + 250, recent_context_tokens=100, reactive_recent_context_tokens=50)))
            save(directory / 'frozen-input.json', dict(config=asdict(engine.config), context=plain(engine.context_manager), pins=pins))
            summary = '<summary>当前目标：继续资料核对。重要发现：\n' + '\n'.join(f['quote'] for f in facts if f['channel'] == 'SUMMARY_REQUIRED') + '\n剩余工作：根据当前事实完成交接。</summary>'
            recorder = StreamRecorder(evidence, summary=[summary])
            observer = OfflineObserver(SimpleNamespace(responses=recorder, base_url='offline://fake'), directory / 'provider-calls.json')
            with patch('hexgent.loop.agent_turn.get_model', return_value='FAKE_ONLY'):
                events = list(engine.run(latest, client=observer))
            mains = [r['request'] for r in evidence['requests'] if r['kind'] == 'MAIN']
            comps = [r['request'] for r in evidence['requests'] if r['kind'] == 'COMPACTION']
            transcript = engine.session.store.read_transcript()
            protected = all(f['quote'] in mains[0]['instructions'] for f in facts if f['channel'] == 'EXTERNALLY_PROTECTED') if mains else False
            ok = bool(comps and mains and engine.context_manager.compaction_count and protected)
            # 3. Fake usage 只保留协议样本；真实计费与质量结果明确不可用。
            for row in observer.turns:
                row.update(call_id=f"{case['id']}:call:{row['turn_id']}", fixture_id=case['id'],
                    session_id=engine.session.store.session_id, evidence_type='FAKE_PROTOCOL_ONLY',
                    provider_input_tokens=None, provider_output_tokens=None, provider_usage_status='NOT_APPLICABLE')
            save(directory / 'evidence.json', dict(requests=evidence['requests'], calls=observer.turns,
                events=plain(events), transcript=transcript, context=plain(engine.context_manager),
                external_protection_verified=protected, candidate_summary=summary,
                real_quality_result='NOT_EVALUATED'))
            results.append(dict(id=case['id'], status='PASS' if ok else 'FAIL', compaction_calls=len(comps),
                main_calls=len(mains), compaction_count=engine.context_manager.compaction_count,
                external_protection_verified=protected))
        except Exception as error:
            save(directory / 'failure.json', dict(type=type(error).__name__, error=str(error), evidence=evidence))
            results.append(dict(id=case['id'], status='FAIL', reason=str(error)))
    save(ROOT / 'runner_dry_run.json', dict(output=str(output), results=results, paid_calls=0, production_unchanged=before == hashes(),
        gate='BLOCKED_FOR_C2', blocker='真实 Provider 计价、失败计费和请求上限未确认；离线结果不能证明实际 Usage 完整'))


if __name__ == '__main__':
    main()
