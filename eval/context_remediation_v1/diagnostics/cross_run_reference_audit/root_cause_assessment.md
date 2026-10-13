# 原因与证据等级

CONFIRMED：六响应52facts全部身份、prefix切点形式合法；完整前缀支持各事实，但6个Fact自身引用不覆盖其声明范围。
CONFIRMED：Pilot10 f6旧记录0缺口，L3-R2 f6旧记1；同类端点全称问题历史漏检。其他集合证据否定/组成命题也曾放行，导致跨Run计数不一致。
CONFIRMED：validate_snapshot仅校验Schema/身份/切点/旧记录处置和预算，不进行语义覆盖（key_fact_snapshot.py:43–81,115–127）。不是实现偏离已承诺语义。
STRONGLY_SUPPORTED：模型对正确的全历史概括使用稀疏代表性refs，审核又有时把全前缀正确等同refs充分。
HYPOTHESIS：Prompt要求原子事实但未明确自身refs范围/全称证据，可能促进代表性引证；无对照不能证实因果或v3有效。
NOT_SUPPORTED：无来源事实增多、切点异常、Runtime恢复问题、Lost-in-the-Middle或模型能力普遍不足。
本次不是修复，没有模型调用。
