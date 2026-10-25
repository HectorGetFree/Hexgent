"""固定配置型Variant；不改变任务合同、权限或预算，不提供任意覆盖DSL。"""
from dataclasses import asdict, replace
from hashlib import sha256
import json

VARIANTS = ('baseline', 'no_memory_retrieval', 'tool_parallel_on')


def effective_config(budget, variant_id='baseline'):
    from .baseline_support import full_config
    if variant_id not in VARIANTS:
        raise ValueError('不支持的Variant：' + str(variant_id))
    config = full_config(budget)
    if variant_id == 'no_memory_retrieval':
        return replace(config, memory_retrieval=replace(config.memory_retrieval, enabled=False))
    if variant_id == 'tool_parallel_on':
        return replace(config, tool_parallel=replace(config.tool_parallel, enabled=True))
    return config


def metadata(budget, variant_id):
    config = asdict(effective_config(budget, variant_id))
    digest = sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return dict(variant_id=variant_id, variant_schema_version=1,
                effective_config=config, effective_config_sha256=digest)


def exclusion(contract, variant_id):
    effective_config(contract.execution_budget.model_dump(), variant_id)
    if variant_id == 'no_memory_retrieval' and contract.task_id == 'T05' and contract.scenario_config.get('measurement_scope') == 'hexgent-memory-mechanism-specific':
        return 'NOT_APPLICABLE: T05明确要求Memory机制，关闭召回与原评分合同冲突'
    return None


def require_applicable(contract, variant_id):
    reason = exclusion(contract, variant_id)
    if reason:
        raise ValueError(reason)
    if contract.verifier_spec.verifier_version != '2':
        raise ValueError('显式Variant仅支持F1/协议v2')


def config_toml(config):
    """现有配置仅含标量；交给Production load_config做最终类型校验。"""
    sections = []
    for section, values in asdict(config).items():
        lines = ['[' + section + ']']
        for key, value in values.items():
            if type(value) not in (str, bool, int, float):
                raise ValueError('配置包含非标量字段')
            lines.append(key + ' = ' + json.dumps(value, allow_nan=False))
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections) + '\n'


def verify_actual(records, expected, model):
    if not records or any(r['config'] != expected or r['model'] != model for r in records):
        raise ValueError('实际Runtime配置/模型不符合冻结Variant')


def trial_id(campaign_root, variant_id, case_id, ordinal):
    from pathlib import Path
    from pydantic import TypeAdapter
    from .contracts import Identifier
    if variant_id not in VARIANTS or ordinal not in (1, 2, 3):
        raise ValueError('非法Variant或Trial序号')
    campaign = sha256(str(Path(campaign_root).resolve()).encode()).hexdigest()[:16]
    value = f'v1-{campaign}-{variant_id}-{case_id}-{ordinal}'
    return TypeAdapter(Identifier).validate_python(value)
