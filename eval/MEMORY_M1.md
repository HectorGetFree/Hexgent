# Memory M1 — 显式跨 Session 事实回答 ON/OFF Smoke

## 执行前审计

AgentEngine.submit → MemoryRetriever(config.memory_retrieval).retrieve → LoopState.relevant_memory
→ build_context_details 去重/预算筛选 → call_model 的最终 memory_injected 凭证。
现有 MemoryRetrievalLimits.enabled=false 返回空列表，既有 RunOptions/HexgentConfig 可以控制，
不需要生产 feature flag。ContextRecord、Compression、工具、模型参数均不随 OFF 改变。

默认 CLI 的 Memory 和旧 Transcript 都位于可读工作区，因此仅关闭 enabled 不足以隔离。
Eval 使用 AgentEngine 已有 memory_store 依赖注入：

case scope
├── WRITE/workspace：写入 Session，仅使用生产 remember
├── WRITE/vault：生产 MemoryStore 的正式持久化
├── ON/vault、OFF/vault：同一 Memory 快照的独立克隆
└── ON/workspace、OFF/workspace：空白查询工作区、新 Engine、新 Session

两侧统一使用生产 dont_ask：允许内部只读与 Plan/remember；禁止访问外部 Store、旧 Session，
禁止未预批准 Shell。不修改权限规则或 Handler。查询工作区没有答案/提示 A/Oracle/旧 ContextRecord。
这不是 OS sandbox，也不是默认 CLI 文件布局的安全性评测。
严格比较 Snapshot，查询修改 Store、出现未经证实的文件结果或额外来源时停止。
Scope Oracle v2 允许经原始参数及结果证明未返回事实的空本地 glob，以及事先固定的空白
pyproject 标记读取；这不关闭其他 Agent 能力，也不把普通工具使用误判为记忆泄漏。
非空 glob、未知文件读取、缺失调用参数仍需阻塞，不凭模型自述认定无泄漏。
OFF 单独改变 memory_retrieval.enabled；同样的其他能力/权限边界在 ON 也生效。

## 六个预注册案例

| ID | 类别 | 合成实体 | Ground Truth |
| --- | --- | --- | --- |
| M1-01 | 用户偏好 | Veyra-K8L | 边界先述 → 反例核对 → 落地清单 |
| M1-02 | 项目约束 | Zuno-R4Q | 只能青铜工单-73，禁止邮件 |
| M1-03 | 技术配置 | Kestrel-T9N | port=48271，codec=lz4-block-v2 |
| M1-04 | 数值事实 | Maru-N6S | burst=731，window_seconds=19 |
| M1-05 | 多条件 | Orin-V2D | 岚岛且等级≥4 → 银组-58，其他 → 观察队-26 |
| M1-06 | 易混淆 | Luma-A7C / Luma-A7G | amber-41 / navy-86，不得交换 |

每例一次真实写入、一次 ON、一次 OFF；奇数 ON→OFF，偶数 OFF→ON。
只评测显式 remember，不声称自动提取。唯一虚构实体及任意取值不依赖公共知识。
所有原文、问题、答案、顺序和生产配置在 prepare 时冻结到 manifest。

## 五层 Oracle

1. Write：真实成功 remember callId/result ID → 正式 MemoryItem，source=explicit；
   完整事实原文及关联必须保存（写入 query 明确要求不改写）。不能只检查词袋。
2. Retrieval：读取已校验、已提交的 run_finished Checkpoint.relevant_memory，独立于注入凭证。
3. Injection：成功 MAIN_AGENT 请求的最终 ID/content hash 凭证，与写入内容一致。
4. Answer：相同 JSON {value:...} 和相同 Gold；null 表示不知道，不当作正确答案。
5. Scope：新 Context/Record、快照一致、生产权限探针、无外部工具来源；OFF 召回与所有注入为空。

注入成功不等于回答正确。Malformed 答案不能擅自归为 unsupported=false。
Unsupported：断言非 null，但没有提供的事实依据或与 Gold 不符；OFF 猜中也标记无已知依据。
这是可审计的闭域判定，不声称衡量模型所有内部知识。

## 指标口径

ON Grounded Recall Accuracy = 正确回答且正确注入 / 分母。
OFF Answer Accuracy = 正确回答 / 同一分母。
Memory Lift = (ON accuracy − OFF accuracy) ×100，单位百分点。
全部预注册分母固定 6，尚未执行/无有效证据只记“未证明成功”，不是完整成功率估计；
另报告有效执行配对，须 terminal completed、Scope 正常、JSON 可判定、Provider/model 一致。
不隐藏写入失败、模型错误或 Provider 请求失败。

Provider Token = 全部 MAIN + Auxiliary 请求实测 usage 合计；缺失保持 null，保留已知小计。
配对 Token Overhead 和 Latency Difference 均为 ON−OFF，不把估算 Token 冒充实测。
单次采样易受网络和模型随机性影响，成本差异不等于纯注入开销的因果估计。
生产自动恢复的短暂请求错误保留，不因最终成功而删除；未恢复错误停止执行。

## 运行

```sh
cd /Users/hector/dev/hexgent
export M1_OUTPUT="$PWD/eval/memory_m1/runs/m1-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m eval.memory_m1 prepare --output-dir "$M1_OUTPUT"
.venv/bin/python -m eval.memory_m1 smoke --output-dir "$M1_OUTPUT" --execute
# 只有前两例隔离/检索/注入及观测通过后：
.venv/bin/python -m eval.memory_m1 remaining --output-dir "$M1_OUTPUT" --execute
.venv/bin/python -m eval.memory_m1 report --output-dir "$M1_OUTPUT"
```

真实调用走当前生产 client（.env 可能覆盖 export），没有 Provider 包装器或固定模型回答。
每次结果保留原始 Session 路径、Checkpoint、观测、oracle、usage。已有阶段目录拒绝重新执行。
历史 Memory Hardening Artifact 不受影响。
若停止，不能自动重跑或用成功样本替换；先解释原始失败，再决定独立补充实验。
六例完成仍只是 M1 Smoke，不能宣传广泛稳定性。

## 已有 Scope 误判的只读修正

旧 Scope Oracle 将所有成功非 Plan 调用都当作污染，错误拒绝了无结果的本地 glob。
不改任务、答案、Memory 开关或生产代码。`recheck-scope` 从 durable Transcript 读取
Call ID/原始参数及结果，与原 Snapshot、最终请求凭证交叉核对。
新增 scope-correction.json（包含原始 result.json SHA、旧判定、修正理由）；不覆盖旧结果。
后续汇总引用两者；已完成的原始模型轨迹不重跑、不替换。

```sh
.venv/bin/python -m eval.memory_m1 recheck-scope --output-dir "$M1_OUTPUT" \
  --case M1-03 --variant OFF
```
