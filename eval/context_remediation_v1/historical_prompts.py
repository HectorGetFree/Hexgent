"""只供历史实验复现：读取冻结请求，不随 Production 默认版本漂移。"""
from hashlib import sha256
import json
from pathlib import Path

_REQUESTS=Path(__file__).resolve().parent/'runs/extractor-quality-pilot-v1-20261012/requests'

def _load(arm, expected):
    prompt=json.loads((_REQUESTS/f'{arm}.json').read_text())['instructions']
    if sha256(prompt.encode()).hexdigest()!=expected:
        raise RuntimeError('历史冻结Prompt哈希不符')
    return prompt

V1=_load('00','8eb8eeb6ab28c8006bae6c0f3da8c07999ed910b15c0972dfdc4ff97e6102d33')
V2=_load('10','4eabf33ac2ee71a095bea0cea62b5dc130da14a0924db681301a796e9adeeef6')
