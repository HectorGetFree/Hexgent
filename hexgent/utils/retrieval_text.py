"""供 ContextRecord 与 Memory 共用的词法召回文本处理。"""

import re


def normalize_retrieval_text(value: str) -> str:
    """规范化文本空白与大小写，供短语匹配使用。"""
    return " ".join(value.lower().split())


def tokenize_retrieval_text(text: str) -> set[str]:
    """提取路径、代码标识符、英文词与中文二三元片段。"""
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_./\\-]+", text):
        lowered = raw.lower().strip("./\\-")
        if len(lowered) >= 2:
            tokens.add(lowered)
        for part in re.split(r"[\s_./\\-]+", raw):
            for component in re.findall(
                r"[A-Z]+(?=[A-Z][a-z]|\d|$)|[A-Z]?[a-z]+|\d+",
                part,
            ):
                component = component.lower()
                if len(component) >= 2:
                    tokens.add(component)
    for chinese in re.findall(r"[\u3400-\u9fff]+", text):
        if len(chinese) >= 2:
            tokens.add(chinese)
        for size in (2, 3):
            tokens.update(
                chinese[index:index + size]
                for index in range(len(chinese) - size + 1)
            )
    return tokens


def retrieval_token_weight(token: str) -> float:
    """提高路径和代码标识符的匹配权重。"""
    if any(separator in token for separator in ("/", "\\", ".", "_", "-")):
        return 3.0
    if re.fullmatch(r"[a-z0-9]+", token):
        return 1.5 if len(token) >= 3 else 1.0
    if len(token) >= 4:
        return 2.0
    if len(token) == 3:
        return 1.0
    return 1.0


def is_context_reference_query(text: str) -> bool:
    """识别可能依赖上文指代的短 Query，不把它误当作语义理解。"""
    normalized = normalize_retrieval_text(text)
    markers = ("它", "这个", "那个", "前面", "之前", "上次", "上述", "继续")
    if not normalized or not any(marker in normalized for marker in markers):
        return False
    # 明确路径、代码标识符或较长描述仍优先走普通词法召回。
    return len(normalized) <= 40 and not re.search(r"[a-zA-Z0-9_./\\-]{3,}", normalized)
