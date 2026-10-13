"""T01–T03 的确定性业务语义验证；不写盘、不调用模型或业务 API。"""

import json
from pathlib import Path

from .contracts import ArtifactReference, AssertionResult, VerifierResult
from .verifier import FieldEqualityVerifier, Observation, VerificationInput


def parse_delivery(text):
    """仅接受一个 JSON 对象；代码围栏是格式包装，不作自由文本语义猜测。"""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError("JSON 字段重复")
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=unique,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def same_json(actual, expected):
    if isinstance(expected, list):
        # 验收项是无序集合，但不接受重复项或错误类型。
        if not isinstance(actual, list) or len(actual) != len(expected): return False
        return sorted(json.dumps(v, sort_keys=True) for v in actual) == sorted(json.dumps(v, sort_keys=True) for v in expected)
    if isinstance(expected, dict):
        return isinstance(actual, dict) and set(actual) == set(expected) and all(same_json(actual[k],v) for k,v in expected.items())
    return type(actual) is type(expected) and actual == expected


class BusinessCaseVerifier:
    """把可核对的业务证据转换为布尔断言，再复用 B1-A 结果协议。"""

    def verify(self, inputs: VerificationInput) -> VerifierResult:
        try:
            return self._verify(inputs)
        except (KeyError, TypeError, ValueError, IndexError):
            return FieldEqualityVerifier().verify(inputs.model_copy(update={
                "business_state":Observation(values={},evidence={})}))

    def _verify(self, inputs: VerificationInput) -> VerifierResult:
        path = inputs.artifact_root / "observations/business-evidence.json"
        reference = ArtifactReference(path="observations/business-evidence.json")
        empty = Observation(values={}, evidence={})
        try:
            # 避免根目录内的符号链接跳出 Trial。
            if not path.resolve().is_relative_to(inputs.artifact_root.resolve()): raise ValueError("证据路径越界")
            evidence = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return FieldEqualityVerifier().verify(inputs.model_copy(update={"business_state":empty}))
        identity=(evidence.get("suite_id"),evidence.get("case_id"),evidence.get("trial_id"),evidence.get("contract_sha256"))
        if identity != (inputs.trial.suite_id,inputs.trial.case_id,inputs.trial.trial_id,inputs.contract.digest()):
            return VerifierResult(schema_version=1,suite_id=inputs.trial.suite_id,case_id=inputs.trial.case_id,
                trial_id=inputs.trial.trial_id,status="INVALID",failure_category="protocol",verifier_version="1",
                assertions=(AssertionResult(assertion_id="business_identity",category="protocol",status="FAIL",
                    evidence=(reference,),detail="业务观测与冻结 Case/Trial 不匹配"),))
        values = {}
        required = {"initial", "current", "stages", "versions", "mutations", "calls", "settled"}
        if not required <= set(evidence):
            return FieldEqualityVerifier().verify(inputs.model_copy(update={"business_state":empty}))
        initial, current = evidence["initial"]["documents"], evidence["current"]["documents"]
        originals = {d["document_id"]:d for d in initial}
        present = {d["document_id"]:d for d in current}
        values["seed_unchanged"] = all(identifier in present and all(
            present[identifier][key] == document[key] for key in ["file_name","status","version_id","content_hash"])
            for identifier,document in originals.items())
        calls=evidence["calls"]
        values["tool_scope"] = all(call["tool_name"] in inputs.contract.allowed_tools
                                    for call in calls if call["status"] in {"success","deduplicated"})
        for call in calls:
            if call["tool_name"]=="read_file" and call["status"]=="success":
                read_path=str(call["arguments"].get("path",""))
                values["tool_scope"] = values["tool_scope"] and "/tool-results/" in read_path and (
                    read_path.startswith(".hexgent/sessions/") or read_path.startswith("/workdir/.hexgent/sessions/"))
        task=inputs.contract.task_id
        oracle=inputs.contract.expected_outcome
        if task=="T01":
            delivery=parse_delivery(evidence["stages"][-1].get("answer")) if evidence["stages"] else None
            if delivery is not None:
                fact_keys=set(oracle["final_document"])
                values["facts"] = set(delivery)==fact_keys|{"sources"} and same_json(
                    {k:delivery.get(k) for k in fact_keys},oracle["final_document"])
                values["facts"] = values["facts"] and not any(
                    delivery.get(key) in wrong for key,wrong in oracle["forbidden_facts"].items())
            else:
                values["facts"]=False
            sources=delivery.get("sources") if delivery else None
            used=set()
            for call in calls:
                if call["status"]!="success": continue
                data=call.get("data") or {}
                if call["tool_name"]=="get_document": used.add((data.get("documentId"),data.get("versionId")))
                elif call["tool_name"]=="search_workspace":
                    used.update((item.get("documentId"),item.get("versionId")) for item in data.get("items",[]))
            by_name={d["file_name"]:d for d in initial}
            valid=isinstance(sources,dict) and set(sources)==set(oracle["required_source_files"])
            for group,name in oracle["required_source_files"].items():
                rows=sources.get(group) if isinstance(sources,dict) else None
                expected=by_name.get(name)
                valid=valid and isinstance(rows,list) and bool(rows) and expected is not None
                if isinstance(rows,list) and expected:
                    for row in rows:
                        valid=valid and isinstance(row,dict) and set(row)=={"documentId","versionId"}
                        if isinstance(row,dict):
                            pair=(row.get("documentId"),row.get("versionId"))
                            valid=valid and all(type(v) is int for v in pair) and pair==(
                                expected["document_id"],expected["version_id"]) and pair in used
            values["sources"]=bool(valid)
            values["side_effects"] = set(present)==set(originals) and not evidence["mutations"] and len(evidence["versions"])==len(initial)
        elif task in {"T02","T03"}:
            target=[d for d in current if d["file_name"]==oracle["output_file"]]
            values.update(facts=False,intermediate=False,version_chain=False,cas=False,side_effects=False)
            if len(target)==1:
                document=target[0]
                versions=[v for v in evidence["versions"] if v["document_id"]==document["document_id"]]
                versions.sort(key=lambda v:v["version_no"])
                values["version_chain"] = len(versions)==2 and [v["version_no"] for v in versions]==[1,2] and (
                    document["version_id"]==versions[-1]["version_id"] and document["status"]==2 and all(v["object_checksum_valid"] for v in versions))
                if versions:
                    values["facts"] = same_json(parse_delivery(versions[-1].get("content")),oracle["final_document"])
                    values["intermediate"] = same_json(parse_delivery(versions[0].get("content")),oracle["initial_document"])
                # 中间产物必须在第一阶段完成时存在，而非末尾一次性伪造两个版本。
                if len(evidence["stages"])<2:
                    values["intermediate"]=False
                else:
                    first=[d for d in evidence["stages"][0]["observation"]["documents"] if d["file_name"]==oracle["output_file"]]
                    values["intermediate"] = values["intermediate"] and len(first)==1 and first[0]["version_no"]==1
                writes=[m for m in evidence["mutations"] if m["status"]==1]
                values["side_effects"] = set(present)-set(originals)=={document["document_id"]} and (
                    len(writes)==2 and {m["capability"] for m in writes}=={"create_document","update_document"} and
                    all(m["document_id"]==document["document_id"] for m in writes))
                if any(m["status"] in {0,3} for m in evidence["mutations"]):
                    values.pop("side_effects",None)
                updates=[c for c in calls if c["tool_name"]=="update_document" and c["status"]=="success"]
                values["cas"] = len(versions)==2 and len(updates)==1 and (
                    updates[0]["arguments"].get("documentId")==document["document_id"] and
                    updates[0]["arguments"].get("expectedVersionId")==versions[0]["version_id"] and
                    (updates[0].get("data") or {}).get("previousVersionId")==versions[0]["version_id"] and
                    (updates[0].get("data") or {}).get("versionId")==versions[1]["version_id"])
                if not evidence["settled"]:
                    # 未确认最终版本解析／索引终态属于证据不足，不视为假成功。
                    values.pop("facts",None)
        else:
            return FieldEqualityVerifier().verify(inputs.model_copy(update={"business_state":empty}))
        observation=Observation(values=values,evidence={key:(reference,) for key in values})
        return FieldEqualityVerifier().verify(inputs.model_copy(update={"business_state":observation}))
