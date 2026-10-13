"""适配新增提取协议，不改变原 Runtime Oracle。

calls 继续表示原 MAIN/SUMMARY 队列，extraction_calls 独立保存所有新增请求。
零 Usage 仅是原用量断言的合成夹具，不代表 Provider 实测成本。
"""
import json
from types import SimpleNamespace
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS


def extraction_event(owner, request):
    if request.get("instructions") != EXTRACTION_INSTRUCTIONS:
        return None
    owner.extraction_calls = getattr(owner, "extraction_calls", []) + [request]
    payload = json.loads(request["input"][0]["content"])
    source = payload["history_prefix"][-1]
    message = source["message"]
    content = message.get("text") or message.get("output") or message.get("content") or "历史记录"
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    facts = [dict(id="fixture-fact", content=content[:16], source_refs=[source["message_id"]], status="active")]
    dispositions = []
    if payload["previous_snapshot"]:
        facts = payload["previous_snapshot"]["facts"]
        dispositions = [dict(old_fact_id=f["id"], new_fact_id=f["id"], action="CARRY_FORWARD", source_refs=[]) for f in facts]
    value = dict(as_of_message_id=payload["as_of_message_id"], facts=facts, dispositions=dispositions)
    response = SimpleNamespace(output_text=json.dumps(value, ensure_ascii=False), usage=SimpleNamespace(input_tokens=0, output_tokens=0))
    return iter([SimpleNamespace(type="response.completed", response=response)])


class WithExtraction:
    def __init__(self, original):
        self.original = original
        self.calls = original.calls
        self.extraction_calls = []

    def create(self, **request):
        stream = extraction_event(self, request)
        return stream if stream is not None else self.original.create(**request)
