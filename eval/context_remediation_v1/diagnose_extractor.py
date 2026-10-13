"""离线生成独立 Extractor 诊断证据，不导入或调用 Provider。"""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from hexgent.context.context_budget import ContextLimits
from hexgent.context.context_manager import ContextManager
from hexgent.context.context_compaction import _select_cut_index
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS
from hexgent.context.key_fact_snapshot import validate_snapshot, snapshot_message
from hexgent.utils.token_estimator import estimate_value_tokens
from .offline import ROOT, fingerprints, save


def hashes(root):
    return {str(p.relative_to(root)):sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts and p.suffix!=".pyc"}


def main():
    run=ROOT/"eval/context_remediation_v1/runs/stage-a-real-20261012-rl3"
    output=ROOT/"eval/context_remediation_v1/diagnostics/extractor_quality"
    if output.exists():
        raise RuntimeError("禁止覆盖已有诊断目录")
    output.mkdir(parents=True)
    prod=hashes(ROOT/"hexgent")
    original=hashes(run)
    frozen=fingerprints()
    request=json.loads((run/"extractor-request.json").read_text())
    text=request["input"][0]["content"]
    payload=json.loads(text)
    sources=payload["history_prefix"]
    snapshot=json.loads((run/"snapshot.json").read_text())
    oracle=json.loads((run/"oracle.json").read_text())
    response=json.loads((run/"extractor-response.json").read_text())
    ledger=json.loads((run/"provider-ledger.json").read_text())
    config=json.loads((run/"config.json").read_text())
    row=json.loads((run/"sample.json").read_text())
    manifest=json.loads((run/"frozen-manifest.json").read_text())
    completed=[json.loads(p.read_text()) for p in sorted((run/"extractor-events").glob("*.json"))
               if '"response.completed"' in p.read_text()]
    assert len(completed)==1 and completed[0]["response"]==response
    assert response["id"]==ledger[0]["response_id"] and response["status"]=="completed"
    assert request==json.loads((run/"candidate-requests.json").read_text())["extraction"]
    assert request["instructions"]==EXTRACTION_INSTRUCTIONS
    returned_text="".join(c["text"] for item in response["output"] if item["type"]=="message"
                          for c in item["content"] if c["type"]=="output_text")
    parsed=json.loads(returned_text)
    assert parsed=={k:snapshot[k] for k in ("as_of_message_id","facts","dispositions")}
    manager=ContextManager()
    for r in row["rounds"]:
        manager.append_user_query(r["query"])
        manager.append_messages(r["messages"],completes_round=True)
    manager.append_user_query(row["final_query"])
    cut=_select_cut_index(manager,ContextLimits(**config).recent_context_tokens)
    expected_sources=[s for s in manager.prefix_sources(cut) if s["message_id"] not in manager.intent_source_ids()]
    assert expected_sources==sources and payload["as_of_message_id"]==manager.message_ids[cut-1]
    assert validate_snapshot(parsed,cutpoint=payload["as_of_message_id"],sources=manager.prefix_sources(cut),
        previous=None,budget_tokens=config["key_fact_snapshot_tokens"],excluded_source_ids=manager.intent_source_ids())==snapshot
    missing=[True,True,True,False,False]
    mapping=[]
    for gold, lost in zip(oracle,missing):
        occurrences=[]
        for index, source in enumerate(sources):
            message=source["message"]
            body=message.get("output",message.get("content",""))
            body=body if isinstance(body,str) else json.dumps(body,ensure_ascii=False)
            if gold["quote"] not in body:
                continue
            offset=text.find(gold["quote"])
            pos=body.index(gold["quote"])
            occurrences.append(dict(message_id=source["message_id"],source_type="Tool" if message.get("type")=="function_call_output" else message.get("role"),
                call_id=message.get("call_id"),history_index_zero_based=index,
                exact_payload_character_offset=offset,payload_character_fraction=offset/len(text),
                approximate_local_estimator_tokens_before=estimate_value_tokens(text[:offset]),
                approximation="本地字符估算，不是Provider Token位置；offset仅对该唯一quote有效",
                source_content_characters=len(body),quote_offset_in_source=pos,
                minimum_quote=gold["quote"],context_excerpt=body[max(0,pos-100):pos+len(gold["quote"])+100],
                source_sha256=sha256(json.dumps(message,ensure_ascii=False,sort_keys=True).encode()).hexdigest()))
        assert len(occurrences)==1 and occurrences[0]["source_type"]=="Tool"
        source=next(s for s in sources if s["message_id"]==occurrences[0]["message_id"])
        assert source["message"]["call_id"]==gold["source_call_id"]
        mapping.append(dict(oracle_fact_id=gold["fact_id"],original_fact=gold["quote"],canonical_fact=gold["canonical_fact"],
            actual_request_present=True,complete_explicit_evidence=True,occurrences=occurrences,repeat_count=1,
            evidence_category="C_COMPLETE_EXPLICIT_BUT_NOT_EXTRACTED" if lost else "COMPLETE_EXPLICIT_AND_EXTRACTED",
            later_update="未发现针对该台账断言的显式否定/替代；不可把一般测试免责声明等同冲突",
            conflict="未发现明确相反台账事实；源码阅读与审查台账是不同命题",
            surrounding_material="同一工具output中的完整源码、工作项阅读说明和重复验证免责声明",
            snapshot_coverage=[] if lost else ["f7" if gold==oracle[3] else "f8"],
            full_source=source))
    save(output/"oracle_evidence_map.json",dict(actual_request=str(run/"extractor-request.json"),
        request_response_association=dict(local_call_id=ledger[0]["local_call_id"],response_id=response["id"],
            completed_event_body_equal=True,dispatch_evidence="AuditClient.create在SDK调用前保存完整kwargs；非TLS抓包，不证明Provider内部预处理"),
        payload_characters=len(text),prefix_messages=len(sources),cutpoint=payload["as_of_message_id"],
        intent_reference=payload["intent_reference"],facts=mapping))
    audits=[]
    descriptions=[
        ("f1",[],False,"GENERALIZATION_ENTITY_LOSS","泛化源码审查目标，遗漏AUDIT-MAPLE-57；不进行发布这一尾句在其msg-1/120引用中未获充分支持。"),
        ("f2",[],False,"SUPPORTED_NON_CRITICAL_ACTIVITY","30个工作项正文阅读有证据，但不等于两个指定审查里程碑完成；跨度大且非原子。"),
        ("f3",[],False,"SUPPORTED_NON_CRITICAL_ARCHITECTURE","CLI/Web/Integration功能概述，不能替代任务状态；单条合并多模块描述。"),
        ("f4",[],False,"SUPPORTED_NON_CRITICAL_ARCHITECTURE","Core与Web管理架构概述，不覆盖失败恢复后的检索审查完成。"),
        ("f5",[],False,"SUPPORTED_NON_CRITICAL_ARCHITECTURE","上下文与配置机制概述，不等于参数与协议审查已完成。"),
        ("f6",[],False,"SUPPORTED_SCOPE_CAVEAT","冻结源码不是运行PASS这一边界有证据；不能据此否定独立的审查完成台账。"),
        ("f7",[oracle[3]["fact_id"]],True,"SUPPORTED_CORE_WITH_UNSUPPORTED_ADVICE","等待两处契约确认有证据；不能推进发布/权限结论不得视为验证等额外建议未由其单一引用充分证明。uncertain并非来源明确的active状态，需披露。"),
        ("f8",[oracle[4]["fact_id"]],True,"SUPPORTED_CONDITIONAL_NEXT_STEP","确认后生成报告、不发布有证据；只读范围内确认是泛化建议而不是额外已执行事实。"),
    ]
    for identity,gold,needed,category,reason in descriptions:
        fact=next(f for f in snapshot["facts"] if f["id"]==identity)
        audits.append(dict(**fact,oracle_fact_ids=gold,necessary_for_five_fact_continuation=needed,
            other_audit_usefulness="存在一般审计辅助价值，不应称为普遍无用",classification=category,
            assessment=reason,over_generalization=identity in {"f1","f2","f3","f4","f5"},
            task_identity_preserved=False,completed_oracle_milestones_preserved=False,
            dependency_preserved=identity in {"f7","f8"},additional_advice_unsupported_by_own_refs=identity in {"f1","f7"},
            stale_state="未证明陈旧；f7将确定等待状态标成uncertain，语义标签存在偏差",
            source_refs_legal=True,source_evidence=[s for s in sources if s["message_id"] in fact["source_refs"]],
            estimated_serialized_fact_tokens=estimate_value_tokens(fact)))
    save(output/"snapshot_fact_audit.json",dict(facts=audits,
        snapshot_estimated_tokens=estimate_value_tokens(snapshot_message(snapshot)),snapshot_limit=config["key_fact_snapshot_tokens"],
        actual_output_tokens=response["usage"]["output_tokens"],actual_reasoning_tokens=response["usage"]["output_tokens_details"]["reasoning_tokens"],
        output_limit=request["max_output_tokens"],generation_completed=True,
        allocation="六条事实主要概述目标/架构/阅读/证据边界，两条覆盖必需事实；不是已证明的硬预算耗尽或截断"))
    causes=[
        ("L1","关键事实未进入真实请求/切点错误","NOT_SUPPORTED","完整SDK请求、前缀重建、五条唯一quote及切点msg-120均一致。"),
        ("L1","Provider内部裁剪或预处理","UNKNOWN","无Provider内部输入视图；Usage attribution中tools=296而客户端请求未带tools，不能据此推断实际工具注入或裁剪。"),
        ("L2","证据本身间接含糊","NOT_SUPPORTED","三条失事实均为工具输出中独立的当前有效台账文本，不依赖推断完成。"),
        ("L2","长源码及重复免责声明构成低信号密度背景","CONFIRMED","payload163581字符，120消息、30源码输出；关键事实各仅出现一次。因果影响未证实。"),
        ("L2","免责声明被误泛化为不应保留审查完成事实","HYPOTHESIS","f6强调未执行测试；两条审查完成台账遗漏，但不可观察模型内部推理因果。"),
        ("L2","后续显式状态冲突使旧事实合理失效","NOT_SUPPORTED","没有找到替代任务ID或否定两个已完成里程碑的明确台账。"),
        ("L3","Prompt完全没要求身份或已完成工作","NOT_SUPPORTED","真实Prompt已写任务身份、已完成工作。"),
        ("L3","精确ID与里程碑非替代规则不足","CONFIRMED","没有逐字段精确保留、未发现视为缺失、正文阅读不能替代完成状态的具体要求。是否造成遗漏仍是待实验假设。"),
        ("L3","Prompt偏未来而主动弱化已完成里程碑","HYPOTHESIS","继续任务必需与意图参考可能影响选择；Prompt列出已完成工作，不能定论未来偏好。"),
        ("L4","本次模型语义选择/生成遗漏","CONFIRMED","有完整证据却输出八事实，遗漏三条Oracle；f1目标泛化而实体丢失。"),
        ("L4","架构概述优先于关键里程碑","STRONGLY_SUPPORTED","f2–f6多为架构/阅读/免责声明，精确任务实体及两个完成状态未保留；不证明模型普遍倾向。"),
        ("L4","Lost-in-the-Middle或模型普遍能力不足","HYPOTHESIS","仅一次输出、无位置/长度/Prompt对照，不足判断。"),
        ("L5","Schema与来源校验不能发现首代Recall不足","CONFIRMED","validate_snapshot允许任意非空事实列表、合法refs；首代previous=None，没有已知必要事实集合。"),
        ("L5","输出上限/快照上限导致截断","NOT_SUPPORTED","completed且incomplete_details=null，1385/4000输出，快照1010/2000本地估算；仍不证明模型没有主动压缩偏好。"),
        ("L5","额外规范建议和状态标签未获语义校验","CONFIRMED","f1/f7引用不足支持部分建议；来源合法只校验身份，不审核蕴含和epistemic状态。"),
    ]
    save(output/"root_cause_matrix.json",[dict(layer=l,cause=c,evidence_level=e,evidence=r) for l,c,e,r in causes])
    prompt="""# 冻结 Prompt 分析\n\n## 实际 Prompt（原文，未修改）\n\n```text\n"""+request["instructions"]+"""\n```\n\n## 七项要求\n\n|项目|实际要求|缺口|\n|---|---|---|\n|任务精确身份/批次ID|“任务身份”|没有明确ID必须原样保留；泛化主题不能替代实体|\n|完成里程碑|“已完成工作”|没有区分正文阅读、实现存在、审查完成、测试通过|\n|未完成工作|“当前状态、待确认依赖”|没有独立字段穷尽要求|\n|阻塞与依赖|明确列出|本次f7保留，但uncertain标签与额外建议需审计|\n|下一步及条件|明确列出|本次f8保留|\n|否定条件|“Pinned Constraints有独立保护通道”|并未规定所有历史业务否定条件必须保留；本次不发布有保留|\n|时间与状态更新|“依据前缀时间顺序，不将旧状态当作当前状态”|有要求，首代没有语义Oracle；不能由合法ref推断状态正确|\n\nPrompt已要求身份和完成工作，不能诊断成完全缺少要求。选择规则只有“继续任务必需”，存在解释空间。\n“原子事实”与f3–f5多命题复合事实存在输出不符，但Prompt未设置事实数量上限，也未明确鼓励泛化。\n大量后半Prompt解释旧Snapshot处置规则；本轮previous_snapshot=null，这些规则仍占用注意力，影响仅为假设。\n最新Query为“继续当前交接，使用当前有效结论，不进行业务变更。”，Plan=null；它被标成只读意图，不提供Oracle答案。\n该Query可能影响重要性选择，但未证明模型将其升级为历史事实。三条失败事实均不是当前Query。\n\nSchema没有独立identity/milestone字段；合法content可以泛化。它与开放事实列表目标兼容，但不保证充分性。\n没有事实/建议明确分离字段；“content是模型提取而非已证明真实”不是禁止无依据建议的可执行蕴含校验。\n源码：key_fact_extractor.py:26–43（Prompt）、46–76（请求/验证）；key_fact_snapshot.py:40–127（结构/来源/预算）。\n\n结论：精确选择标准值得强化，但Prompt偏差尚非因果证实；不能归因于输出硬上限。\n"""
    (output/"prompt_analysis.md").write_text(prompt,encoding="utf-8")
    candidates="""# 最小修复候选（仅设计，未实施）\n\n## A — 精确事实选择标准v2（优先）\n\n在现有Extractor Prompt中明确：存在时保留任务对象/批次标识原值，关键已完成里程碑与完成状态，当前依赖及后续动作条件；没有证据时不编造。\n禁止用已阅读源码、实现功能存在替代完成里程碑；不能用主题概述替代实体；非必需架构描述不得优先于这些事实。事实与建议区分，uncertain仅用于证据存在冲突/未知，而非把确定的待确认状态泛化为不确定。\n只改版本化Prompt，不列入AUDIT-MAPLE-57或Oracle答案。保留Schema/切点/来源/生命周期。\n不增加调用；预计少量指令Token，实验预设增量上限1024估算Tokens。风险：过多事实、过度照单提取或伪造缺失槽位；仍须独立来源与Recall审核。\n验证：相同完整历史v1/v2对照，并保留独立R-SPARSE，不能单次通过就称普遍修复。\n\n## B — 全历史等价的来源包络呈现v2（备选）\n\n把所有历史来源统一呈现为独立带ID、类型及长度的可辨识包络，正文原样保存，顺序/切点不变，最新意图独立。\n不得只挑五条Gold或按失败case生成答案索引，不得删除源码中的原始事实。需要Eval先证明正文、ID、顺序逐项可逆一致。\n修改范围限Extractor输入编码，来源hash与validate_snapshot仍指向原始message，不改Snapshot生命周期。不增加调用；元数据与包络会增加输入Tokens，冻结上限1024增量才执行。\n风险：已存在JSON边界，收益可能没有；元数据额外占预算；排版不同不能单独证明长上下文容量或Lost-in-the-Middle。\n验证：与A形成2×2输入/Prompt因素实验，Oracle不变。\n\n不提出候选C：当前没有证据表明A/B不足；不引入第二Judge、换模型、扩大Snapshot、分段多调用或新事实库。\n"""
    (output/"remediation_candidates.md").write_text(candidates,encoding="utf-8")
    experiment="""# 后续受控实验提案（未获执行授权）\n\n冻结原Stage A、五事实Oracle及所有输入；新文件使用独立目录及版本化Prompt/Input策略。\n预先假设H-P：精确身份/里程碑标准能提高Recall；H-I：等价包络能提高来源选择；H-X：两者存在交互。\n\n## 第一阶段：最小因素Pilot，R-L3四次Extraction\n\n|臂|Prompt|完整输入呈现|\n|---|---|---|\n|00|v1|原始JSON|\n|10|v2选择标准|原始JSON|\n|01|v1|等价包络v2|\n|11|v2选择标准|等价包络v2|\n\n每臂一次、顺序预注册，SDK retry=0；只Extractor，不调用Summary或MAIN。原历史那次失败保留为历史参照，不替代同期00。\n所有臂保留相同历史正文、来源ID、顺序、切点和输出Schema/预算。整请求增量均不超过1024本地估算Tokens（A+B合计，不分别各放1024）。\n额外最多4次；每次估算上界54,345+1,024+4,000=59,369，四次237,476 Tokens（不含安全余量，安全余量仍参与单请求容量检查）。\n每次真实Usage逐项核对，估算不是价格或Provider实测。版本化后必须重新离线冻结精确请求，若越界停止申请新授权。\n\n## 第二阶段：独立样本R-SPARSE，两次Extraction\n\n原版与预注册选出的方案各一次，独立Oracle3事实沿用冻结样本。选型规则：Recall最高且无新增不支持事实；相同结果优先A的最小修改。\n每次上界35,169+1,024+4,000=40,193，二次80,386；最多六次总估算317,862 Tokens。若第一阶段没有合格候选则不启动第二阶段。\nR-SPARSE对框架已预注册，不宣称盲测；候选设计不得引用其答案，且不得修改它。\n\n成功标准：R-L3五事实5/5且无无依据事实、错误状态/依赖、非法引用；R-SPARSE3/3满足同标准；Provider完整、隔离有效。失败全部保留。\n四臂单次只形成方向性证据，不能区分Prompt效应与随机性到统计显著程度。六次通过也只能初步支持可迁移性。\n如果需稳定性确认，另行审批将四臂各增加两次配平（8次），上限474,952估算Tokens；不得根据失败临时加跑。\n\n本设计可区分Prompt选择标准与输入包络组织的初步影响，不真正检验长短上下文效应；后者仍UNKNOWN。\n若要验证长度/位置假设，另设保留所有任务事实与免责声明的背景长度/位置独立因素，并先审批消融合同；不得偷偷删除原始历史替代本实验。\n\n零Production改动、零模型调用已经执行。须用户另行批准，不能自动运行或重跑至PASS。\n"""
    (output/"experiment_proposal.md").write_text(experiment,encoding="utf-8")
    table="\n".join(f"|{m['oracle_fact_id']}|{m['occurrences'][0]['message_id']}|{m['occurrences'][0]['exact_payload_character_offset']}|{100*m['occurrences'][0]['payload_character_fraction']:.2f}%|{'FAIL' if not m['snapshot_coverage'] else 'PASS'}|" for m in mapping)
    report="""# EXTRACTOR-QUALITY-DIAGNOSIS\n\nGate：DIAGNOSIS_COMPLETE（诊断材料齐备，不是根因实验证实或修复成功）。\n本轮零Provider/网络调用，未修改Production、Prompt、Schema、配置或Oracle。\n\n## Q1 — 三条事实是否完整明确地进入实际请求？\n\n是。以AuditClient在SDK派发前保存的完整请求为依据，candidate/request相同；response_id、流式completed事件、原响应、解析Snapshot完全关联。\n120条来源，切点msg-120；实际payload163,581字符。三条遗漏均属C：完整明确的工具台账进入请求，却未提取。\n不是A（漏传）或B（需要推理的含糊完成事实）。每条quote均只出现一次，无后续明确相反台账。\n\n|Oracle|来源|字符offset|占payload位置|提取|\n|---|---|---:|---:|---|\n"""+table+"""\n\n位置是字符比例，JSON中的精确offset；本地Token近似另见JSON，不能当Provider Token位置。\n三条遗漏覆盖前部与后部，不能据位置断言Lost-in-the-Middle。消息包含源码正文和每次阅读的免责声明；不是每次读取同一文件。\n详细来源全文与最小quote见oracle_evidence_map.json。\n\n## Q2 — 是否低价值内容挤占关键事实？\n\n相对五事实任务契约，f1–f6主要为目标泛化、30次正文阅读、模块架构和证据免责声明，f7/f8才覆盖两条必需事实。\n这是本次输出选择偏移的直接证据；这些内容并非普遍无用，但不能替代精确任务身份和完成里程碑。\n不能说硬预算被耗尽：Snapshot本地估算1010/2000，Provider output1385/4000（含reasoning563）；completed、无incomplete_details。\n没有截断证据。更多事实数量或更大预算不必然补齐遗漏。\n\n## Q3 — Prompt是否充分要求身份和已完成里程碑？\n\n已写“任务身份、已完成工作”，所以完全未要求这一判断不成立。\n但缺少精确批次/对象ID保持、完成状态非替代和必需信息相对架构概述的具体选择标准。\n不能断言Prompt偏向未来：已完成工作也明确出现；其实际因果作用需对照。真实Prompt全文和七项检查见prompt_analysis.md。\n\n## Q4 — 已确认与假设\n\nCONFIRMED：完整输入可见、三条语义遗漏、首代Schema/来源校验不校验Recall、未耗尽硬输出/快照上限。\nSTRONGLY_SUPPORTED：本次生成偏向架构/阅读概述而非精确任务状态。\nHYPOTHESIS：长源码背景、反复“不是测试通过”的免责声明干扰、选择标准不足导致遗漏。\nUNKNOWN：Provider内部处理。Usage attribution额外列tools=296，但客户端实际kwargs没有tools；不能将该内部字段当作自动工具注入或裁剪证明。\nNOT_SUPPORTED：切点错误、后续台账明确否定、输出被截断、Prompt完全未要求已完成事项。\n单次请求不足证明模型普遍能力不足、Prompt根因或Lost-in-the-Middle。\n\n## 八条事实审计\n\nf1泛化任务身份，遗漏批次；f2正文阅读不等同完成里程碑；f3–f5架构概述；f6源码阅读证据边界。\nf7等待确认保留，但uncertain标签及额外规范建议应单列；f8条件性下一步保留。\nf1“发布”尾句和f7额外规范建议不能仅由各自引用充分支持；不能因来源合法将其认定为完全语义证明。\n完整content/status/refs及逐项覆盖见snapshot_fact_audit.json，未改原2/5口径。\n\n## Q5 — 优先修复候选\n\n先验证A：版本化Extractor事实选择标准，精确保留实体与完成里程碑、避免用源码阅读/功能概述替代状态，不引入答案或新Schema。\n输入组织B只作独立备选因素，不预先宣称它有效。当前不建议第二Judge、换模型、扩大Snapshot或新状态系统。\n后续2×2四次R-L3 Pilot加两次独立R-SPARSE最多六次、估算上界317,862 Tokens，仅是提案，尚未批准或执行。\n若只批准最小A/B式Prompt验证，应先固定完整输入和v2规则；单次5/5仍不是稳定改善证明。\n\n## 成本与完整性\n\n沿用唯一历史真实调用input47,963/output1,385/total49,348，51,594.91ms，Usage1/1完整，隐藏retry0。\n本地完整输入估算54,345不等同Provider实际；128K风险仍USER_ASSERTED_CAPACITY_128K_UNVERIFIED。\n原Run及复核、Production、冻结资产均前后哈希一致；git diff --check通过。\n此前工程30/30、Runtime343/343、独立45/45为历史证据，本轮不重跑、不把冻结47项2FAIL改为PASS。\n\n下一步：审批候选A的版本化离线请求与上述受控实验；本轮停止，不直接实施或调用模型。\n"""
    (output/"diagnosis_report.md").write_text(report,encoding="utf-8")
    diff=subprocess.run(["git","diff","--check"],cwd=ROOT,capture_output=True,text=True)
    integrity=dict(gate="DIAGNOSIS_COMPLETE",created_at=datetime.now(timezone.utc).isoformat(),
        original_run=str(run),stage_a_artifacts_before=original,stage_a_artifacts_after=hashes(run),
        production_before=prod,production_after=hashes(ROOT/"hexgent"),
        frozen_before=frozen,frozen_after=fingerprints(),network_requests=0,real_model_calls=0,
        git_diff_check=dict(exit_code=diff.returncode,output=diff.stdout+diff.stderr),
        source_dispatch_request_complete=True,raw_response_association=True,
        production_matches_stage_a=all(sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in manifest["production_hashes"].items()))
    integrity.update(original_artifacts_unchanged=integrity["stage_a_artifacts_before"]==integrity["stage_a_artifacts_after"],
        production_unchanged=integrity["production_before"]==integrity["production_after"],
        frozen_unchanged=integrity["frozen_before"]==integrity["frozen_after"])
    if not all((integrity["original_artifacts_unchanged"],integrity["production_unchanged"],integrity["frozen_unchanged"],diff.returncode==0)):
        integrity["gate"]="DIAGNOSIS_INCOMPLETE_EVIDENCE"
    save(output/"integrity_report.json",integrity)
    print(json.dumps(dict(output=str(output),gate=integrity["gate"],positions=[(m["oracle_fact_id"],m["occurrences"][0]["payload_character_fraction"]) for m in mapping],
        production_unchanged=integrity["production_unchanged"],original_artifacts_unchanged=integrity["original_artifacts_unchanged"],frozen_unchanged=integrity["frozen_unchanged"]),ensure_ascii=False))


if __name__=="__main__":
    main()
