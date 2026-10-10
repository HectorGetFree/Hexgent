# Tool Real-Model V3 — Preflight Blocked

状态：BLOCKED_PROVIDER_PREFLIGHT。没有开始 Smoke 或扩大实验。

原始脱敏证据：`runs/v3-preflight-20261008.json`。

使用现有 `eval.phase5c.provider_preflight`、Production `get_client/get_model`，没有更换 Provider、模型、协议或账号。真实执行一次最小 streaming 请求；没有发送源码工作负载，工具列表为空。

已确认：model_id=gpt-6.1-sol；status=FAIL；exception_type=APIError；message=`upstream request failed for model gpt-6.1-sol`；failure_category=PROVIDER_UPSTREAM_REQUEST_FAILED；retryable=false。

该诊断无法确定是模型路由、账号权限、Provider 服务故障或其他上游原因；不得据此归类 policy block、overload 或 account disabled。Artifact 没有足够证据判定 Provider 是否支持多个 Tool Calls，usage availability 也未被本次成功响应验证。一次失败不足以证明“系统性”失败，但已经不满足本轮 Preflight Gate。

| V3 项目 | 状态 |
|---|---|
| Provider Preflight | FAIL |
| 三题各一对 ON/OFF Smoke | NOT_RUN |
| 逐轮调用分布 / 实际并发观测 | NOT_AVAILABLE |
| 任务 Oracle | NOT_RUN |
| Provider Token 完整性 | UNKNOWN |
| 冻结真实轨迹 Serial/Parallel 重放 | NOT_RUN |
| 性能或简历收益结论 | 不可形成 |

本轮没有新增执行次数、自动重试、换模型或修改 Production Runtime。原 V1/V2 证据保留，不以确定性结果补齐 V3 缺失数据。没有运行测试、没有 commit/tag。

恢复条件：当前 Provider 配置的最小 Preflight 成功，再执行三题各一对 Smoke。需使用新的独立输出路径，不覆盖本次失败证据；如果调整模型或 Provider，应记录为新的实验配置。本轮到此停止。
