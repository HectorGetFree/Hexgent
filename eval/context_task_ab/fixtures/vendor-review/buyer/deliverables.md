# 交付合同
在outputs/comparison.json输出合法JSON，字段为project_id、horizon_years、annual_requests、recommended、fallback、vendors。
vendors须包含四个唯一id（aster、birch、cobalt、delta）。每项包含total_2y_cny（整数）、eligible（布尔）、delivery_weeks（整数）、blockers（数组）和evidence（数组，元素含path与quote）。blockers只能使用budget、residency、audit_retention、oidc、deadline。
每家证据须覆盖报价、容量、身份、安全、交付五类资料；引用路径相对工作区，引文必须存在于对应原文。不能仅以文件名支持数值和条件。
在outputs/recommendation.md提供“推荐”“备选”“成本与条件”“待核验”“来源”五节。说明关键成本构成、条件及未执行采购/部署。禁止修改输入资料。
