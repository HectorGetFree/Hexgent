"""本地 JSON 读写；目录合同之外不增加 Artifact 服务。"""

from pathlib import Path
from typing import TypeVar

from .contracts import VersionedModel, canonical_json


Model = TypeVar("Model", bound=VersionedModel)


def write_json(path: Path, value: VersionedModel) -> None:
    """仅由 Evaluator 显式调用；Verifier 不调用此函数。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def read_json(path: Path, model: type[Model]) -> Model:
    return model.model_validate_json(path.read_text(encoding="utf-8"))
