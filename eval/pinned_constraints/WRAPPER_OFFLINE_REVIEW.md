# Pinned Wrapper 离线修正审计

本轮仅修改 Eval，不调用真实 Provider。历史 Smoke Artifact 保持原样。

## 根因及修正

原 RealClient 将完整 JSON 审计副本当作请求发送。SDK reasoning 等对象的未设置字段可能被展开为 null，与 SDK 原生请求序列化不一致。

现在只在 instructions 追加预注册保护区，input、tools 等原始对象直接转发。audit_snapshot 仅供审计，SDK 对象使用 to_dict(mode="json")；不主动删除显式 null，也不通过清除 status 绕过协议问题。请求、流异常记录后原样传播，with_options 保持原参数与共享观测状态。

## 离线证据

- test_multiround_sdk_history_identity_and_wire_equivalence：两轮请求，真实 SDK reasoning/function_call 对象与 function_call_output；原对象身份不变，移除保护区后审计参数等价。
- test_baseline_is_transparent_and_audit_is_independent：Baseline 不改参数，审计副本与调用方可变历史分离。
- test_explicit_null_is_not_removed：不擅自删除实际设置的 null。
- test_options_share_observation_and_forward_unchanged：timeout、max_retries 原样转发。
- test_transport_error_propagates_same_exception / test_stream_failure_and_close_forward：错误对象与关闭调用原样传递。
- test_budget_and_isolation_block_before_native_dispatch：预算或首请求隔离失败时，无 native dispatch。
- E1–E5 七个原确定性子场景继续 PASS。

执行结果：Runtime 276 tests / 0 failures / 0 errors；Pinned、M3、M1/M2、Memory 和 Module Eval 合计 64 tests / 0 failures / 0 errors。git diff --check 无输出。

## 边界与审核结论

READY_FOR_REVIEW。本轮没有真实模型请求，不证明上游接受所有历史轨迹，也不将旧 R1/R2 行为观察升级为合格实验数据。先审计通过，再按审批启动新 Artifact 的真实模型 Smoke。

Production 本轮未编辑；工作区四个 Production 修改属于此前 M3 修复，未撤销或扩展。未更改 M1/M2 实现、固定案例、Oracle、历史 Gate 或真实模型 Artifact。未 commit/tag。
