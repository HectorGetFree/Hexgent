# 配置型模块消融接入

状态：已实现并通过离线验收，尚未执行这些新Variant的真实Smoke或消融批次。仅修改Benchmark，不修改Production、Contract或业务Oracle。

## 支持范围

| variant_id | 唯一配置差异 | 当前可执行范围 |
|---|---|---|
| baseline | 与已有Full基础配置一致 | 10 Case，每Case 3 Trial |
| no_memory_retrieval | memory_retrieval.enabled=false | 9 Case，每Case 3 Trial；T05 single NOT_APPLICABLE |
| tool_parallel_on | tool_parallel.enabled=true，其他并发参数保持基础值 | 10 Case，每Case 3 Trial |

No Memory Retrieval仅关闭召回，不禁用remember和Memory写入。T05 single明确要求Memory机制证据，故在该Variant排除，不修改原评分；T05 multiidentity和T08继续BLOCKED。

不支持no_compaction、summary_only等未有明确Production配置开关的Variant。非法名称立即拒绝。Tool Parallel ON仅允许Production现有声明为并行安全的工具并行；配置开启不保证任务自然产生并发，不宣称一定加速。

## 配置与证据

- `variants.py` 定义固定枚举、基础配置覆盖、TOML序列化、配置SHA-256和Trial ID；没有任意参数DSL。
- Workspace与Fixture使用同一份effective_config。显式Variant的Smoke也采用完整基础配置，避免Smoke与Baseline使用不同轮数/输出上限。
- 每Case保留原合同Hash、Seed Hash、工具权限、预算与Verifier。Manifest记录variant_id、有效配置及摘要；Trial记录同样身份。Trial ID含Campaign路径摘要、Variant、Case和序号，避免同名目录位于不同实验路径时混用。
- Workspace终态归档保留run-config.json；它被纳入Evidence Manifest。配置元数据必须与目标Variant一致，并与原生run_configuration事件逐条匹配。
- 配置未生效/错模型：证据或执行协议无效，不当作任务FAIL。正确关闭模块后任务未完成：沿用原Verifier，保留有效FAIL。
- 每个Variant独立Campaign、每Trial独立环境；没有新增调度系统。

## 执行方法

以下命令仅为用法，本轮没有执行真实模型。工作目录为Hexgent仓库。配置文件中的凭证不复制到报告。

先为目标Variant执行对应Case的新版Smoke，三个入口均新增 `--variant`：

```sh
.venv/bin/python -m eval.benchmark_v1.business_smoke \
  --revision f1 --variant no_memory_retrieval --case T01 \
  --trial-id experiment-a-nomemory-t01-smoke \
  --root eval/benchmark_v1/runs/experiment-a/smokes/no-memory-t01 \
  --workspace-source /Users/hector/dev/hex-workspace \
  --build-metadata eval/benchmark_v1/runs/current-20261018-build/build-metadata.json \
  --docker-socket unix:///Users/hector/.docker/run/docker.sock \
  --provider-env-file .env --embedding-env-file /Users/hector/dev/hex-workspace/.env
```

T01/T02/T03使用business_smoke，T05 single使用memory_business_smoke，其余可执行Case使用fixture_smoke。Fixture不需要Workspace/build/Docker/Embedding参数。No Memory Retrieval不执行T05。

Smoke只要求协议有效，任务FAIL不会被排除。只会选择同Case、同Variant的Protocol PASS Smoke；非Baseline不回退Baseline。Baseline优先显式Variant Smoke，未提供时仍兼容原有未标记Baseline Smoke。存在多个同Variant有效Smoke时拒绝歧义，不按任务成绩择优。历史记录不删除、不覆盖。

完成该Variant所有适用Case的Smoke后：

```sh
.venv/bin/python -m eval.benchmark_v1.baseline \
  --revision f1 --variant no_memory_retrieval \
  --root eval/benchmark_v1/runs/experiment-a/no_memory_retrieval \
  --workspace-source /Users/hector/dev/hex-workspace \
  --build-metadata eval/benchmark_v1/runs/current-20261018-build/build-metadata.json \
  --docker-socket unix:///Users/hector/.docker/run/docker.sock \
  --provider-env-file .env --embedding-env-file /Users/hector/dev/hex-workspace/.env
```

另两组分别使用 `--variant baseline` / `--variant tool_parallel_on` 和不同的新root。每组清单在执行前冻结；禁止在组间改源码、合同、Provider、镜像或其他参数。

上例镜像记录只适用于当前源码仍与记录匹配时；Runner原有源码与镜像校验保留，源码变化应提供新的构建记录，不绕过校验。

## 只读分组比较

```sh
.venv/bin/python -m eval.benchmark_v1.compare_variants \
  eval/benchmark_v1/runs/experiment-a/baseline \
  eval/benchmark_v1/runs/experiment-a/no_memory_retrieval \
  eval/benchmark_v1/runs/experiment-a/tool_parallel_on
```

JSON写到stdout；不修改Campaign和历史结果。比较时：

1. 检查Manifest摘要，要求显式Variant/协议v2，拒绝旧结果静默升级。
2. 核对相同Provider、Production源码、镜像、源码清单、工具Schema；同Case合同、Seed与预算一致。冻结配置只允许预定义Variant差异。
3. 重新读取原始Trial Evidence并运行只读Verifier，核对Trial Record；不信任聚合报告，不进入旧版写入路径。
4. 仅比较共同适用Case，按Case × Variant给出PASS/(PASS+FAIL)、排除样本和已知Token。三组一起比较时T05不进入共同集合，避免Baseline多一个Case导致汇总失真。
5. INVALID/INCONCLUSIVE不进通过率分母；部分完成组显式complete=false，没有有效样本时pass_rate=null，未知成本显式标记。一个Variant仅接收一个Campaign，不静默拼接择优批次。

已有current-20261018-baseline的100%结果保持历史身份；它没有显式Variant元数据，且与本轮Eval代码快照不同，比较器不直接混用。正式消融应在相同新版本上执行一组新的Baseline和目标Variant。

模块未自然触发时，即使ON/OFF结果一致，也不能推断模块没有贡献。每Case仅3次，不宣称统计显著性。不提供自动归因、排行榜或通用表达式系统。

## 离线验收与历史保护

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest eval.benchmark_v1.test_variants
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s eval/benchmark_v1 -t . -p 'test_*.py'
git diff --check
```

新增16项Variant测试，完整145项测试通过。覆盖配置唯一差异、TOML往返、Fixture真实入口的配置构造、Workspace配置生成、各入口参数、实际配置未生效/摘要不符、T05排除与合同保留、有效FAIL、Variant Smoke隔离、共同Case、不可比配置/Provider/代码拒绝、部分结果与未知成本、Trial ID隔离。

原有“缺少新版Smoke拒绝回退”测试改用临时空目录，移除对本地真实产物是否存在的依赖，仍验证相同规则。

修复前后Production `hexgent/` 与历史 `runs/`（排除__pycache__）组合摘要一致：10,748文件，SHA-256 `d2855d18388bdd067b775b250e2df8567c388b67a472e0a1c1d0afe4d79fa230`。未运行真实Provider、Docker或外部业务API，未改HexWorkspace，未提交代码。已有未提交改动保留。
