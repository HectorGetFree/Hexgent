"""Eval-only证据覆盖检查：只处理人工资格确认后的语义模型，不解析自然语言。"""


def entail(requirement, sources, cutoff):
    """返回支持/不支持/未知及证据链；不以信息缺失推出不存在。"""
    if requirement.get("scope_unknown"):
        return "UNCERTAIN", [], [], False
    subjects=requirement["subjects"]
    needed=requirement.get("min_support",len(subjects))
    supported=[]
    missing=[]
    contradiction=False
    uncertain=0
    chains=[]
    for subject in subjects:
        candidates=[]
        unknown_sources=[]
        for source in sources:
            if source["sequence"]>cutoff:
                continue
            for fact in source.get("claims",[]):
                if fact["subject"]==subject and fact["attribute"]==requirement["attribute"]:
                    if source.get("semantic_qualification")!="CONFIRMED":
                        unknown_sources.append(source["id"])
                    else:
                        candidates.append((source.get("effective_at",source["sequence"]),fact["value"],source["id"]))
        if not candidates:
            if unknown_sources:
                uncertain+=1
                chains.append(dict(subject=subject,status="UNCERTAIN",sources=unknown_sources))
            else:
                missing.append(subject)
            continue
        latest=max(item[0] for item in candidates)
        active=[item for item in candidates if item[0]==latest]
        values={repr(item[1]) for item in active}
        if len(values)>1:
            uncertain+=1
            chains.append(dict(subject=subject,status="UNCERTAIN_CONFLICT",sources=[item[2] for item in active]))
        elif active[0][1]==requirement["value"]:
            supported.append(subject)
            chains.append(dict(subject=subject,status="SUPPORTED",sources=[item[2] for item in active]))
        else:
            missing.append(subject)
            contradiction=True
            chains.append(dict(subject=subject,status="CONTRADICTED",sources=[item[2] for item in active]))
    if len(supported)>=needed:
        status="PASS"
    elif len(supported)+uncertain>=needed:
        status="UNCERTAIN"
    else:
        status="FAIL"
    return status,chains,missing,contradiction


def evaluate(case):
    """预期答案在外部文件，本检查器既不读取预期分类也不使用Case ID分支。"""
    byid={s["id"]:s for s in case["sources"]}
    refs=case["source_refs"]
    valid=isinstance(refs,list) and bool(refs) and len(refs)==len(set(refs)) and all(isinstance(r,str) and r in byid and byid[r]["sequence"]<=case["cutoff"] for r in refs)
    cited=[byid[r] for r in refs if r in byid and byid[r]["sequence"]<=case["cutoff"]]
    prefix=[s for s in case["sources"] if s["sequence"]<=case["cutoff"]]
    details=[]
    labels=set()
    for index,requirement in enumerate(case["claim_model"]):
        full,full_chain,full_missing,conflict=entail(requirement,prefix,case["cutoff"])
        own,own_chain,own_missing,_=entail(requirement,cited,case["cutoff"])
        if full=="UNCERTAIN" or own=="UNCERTAIN":
            labels.add("UNCERTAIN")
        if full=="FAIL":
            labels.add("UNSUPPORTED_FACT")
            if conflict:
                labels.add("STATE_ERROR")
        elif full=="PASS" and own=="FAIL":
            labels.add("REFERENCE_GAP")
            if len(requirement.get("subjects",[]))>1:
                labels.add("OVERGENERALIZATION")
        details.append(dict(subclaim_index=index,subclaim=requirement,full_prefix_support=full,
            own_reference_sufficiency=own,full_support_chain=full_chain,cited_support_chain=own_chain,
            full_missing_subjects=full_missing,cited_missing_subjects=own_missing))
    if not valid:
        labels.add("SOURCE_INVALID")
    full="FAIL" if any(d["full_prefix_support"]=="FAIL" for d in details) else "UNCERTAIN" if any(d["full_prefix_support"]=="UNCERTAIN" for d in details) else "PASS"
    own="FAIL" if any(d["own_reference_sufficiency"]=="FAIL" for d in details) else "UNCERTAIN" if any(d["own_reference_sufficiency"]=="UNCERTAIN" for d in details) else "PASS"
    primary=next((kind for kind in ["SOURCE_INVALID","STATE_ERROR","UNSUPPORTED_FACT","REFERENCE_GAP","UNCERTAIN"] if kind in labels),"PASS")
    oracle=case.get("oracle_requirements",[])
    retained=0
    for required in oracle:
        # Oracle保留判定只检查Fact表达是否保留该命题，不能用source里存在答案冒充保留。
        if any(c.get("attribute")==required["attribute"] and c.get("value")==required["value"]
               and set(required["subjects"]).issubset(c.get("subjects",[])) and not c.get("scope_unknown") for c in case["claim_model"]):
            retained+=1
    return dict(source_validity="PASS" if valid else "SOURCE_INVALID",full_prefix_support=full,
        per_fact_reference_sufficiency=own,classification=primary,labels=sorted(labels),
        overgeneralization="OVERGENERALIZATION" in labels,manual_review_required="UNCERTAIN" in labels or case.get("natural_language_manual_review",False),
        subclaim_reviews=details,oracle_retained=retained,oracle_required=len(oracle),
        evidence_scope="人工已确认来源/指代/原子命题后的确定性模型；不是自动自然语言语义验证")
