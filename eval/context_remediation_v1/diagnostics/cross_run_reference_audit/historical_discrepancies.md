# Historical Audit Discrepancies

旧报告、Gate与评分均保持原样。以下是本轮统一复核发现，不能伪装成当时已发现。

|Run|历史缺口|本轮缺口|Fact|分类|
|---|---:|---:|---|---|
|Pilot10|0|2|f6, f13|HISTORICAL_AUDIT_DISCREPANCY|
|Pilot11|0|1|f4|HISTORICAL_AUDIT_DISCREPANCY|
|R-SPARSE-S1|0|0|—|CONSISTENT|
|Stability-L3-R1|0|1|f8|HISTORICAL_AUDIT_DISCREPANCY|
|Stability-L3-R2|1|2|f6, f7|HISTORICAL_AUDIT_DISCREPANCY|
|Stability-SP-R1|0|0|—|CONSISTENT|

Pilot10 f6与Stability L3-R2 f6同类：上述=前面列举的30个工作项，两个独立端点不是全范围汇总。
还发现集合级证据组成/缺失命题缺引证：Pilot10 f13、Pilot11 f4、L3-R1 f8、L3-R2 f7。
核心身份、完成、依赖与条件Oracle事实仍得到支持，但历史“全部额外Fact无引用缺口”的证据需打折，不据此重新宣布旧GatePASS/FAIL。
有类似问题不证明Runtime回归，也不是v2出现了全新的退化；至少有审计漏检/口径执行不一致。
