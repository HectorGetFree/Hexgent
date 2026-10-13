# IS1供应方案比较：P0实施与离线审阅

状态：任务/Oracle/离线机制Gate已落地；真实模型未运行，长上下文工作负载资格尚未通过。

## 核心文件
- `task.py`：公开任务合同，不含推荐答案。
- `fixtures/vendor-review/`：29份只读合成业务资料，8份采购要求与修订、四家各5份专项合同、1份导航。
- `private_oracle/oracle.py`：独立费用公式、资格与来源Oracle，不克隆到Agent工作区。
- `harness.py`：权限边界、压缩前Checkpoint捕获、快照克隆、Eval OFF、尝试预算。
- `offline_worker.py`：两个独立进程正式Restore→Continue，仅Fake客户端。
- `test_gate.py`、`offline_gate.py`：离线验收与不可覆盖报告生成。

## 资料实质性
成本需结合年度许可、两年附加支持/身份、一次性实施/连接器和每年超额请求。预算修订、旧工作量、旧费率可造成过期版本错误；便宜的方案可能因备份驻留不合格，演示期也不能代替完整交付期。
每家资料分为商务、容量、身份支持、安全、交付。没有单一文件给出四家完整成本与推荐答案；允许高效导航，禁止强制全读或重复读取制造长度。
正文约8883本地估算Tokens，26819字节。足以支撑本次比较合同，但**未达到设计候选3–7万源正文Tokens范围，不能宣称自然64K压缩机会已成立**。通用合同说明在各家材料中重复，不计作独立事实或长度收益。

## Gate与口径
7组离线测试通过。空Session的实际read_file结果进入Transcript；在工具批次Checkpoint关闭生成器并冻结；ON/OFF克隆哈希相等，均保持原Session并正式Continue。无压缩场景首请求完全一致。
跨进程ON/OFF压缩探针使用10K主动阈值及较小近期保留/最低收益参数，仅验证工程时序，不是正式任务配置。ON走Production v2提取、摘要、提交；OFF只替换压缩入口、保留预算检查。Fake输出及调用序列不证明真实任务完成或提取质量。
拒绝越界、修改输入、访问Runtime工件和bash；允许正常资料读取。权限边界不是OS sandbox。Provider Usage不适用，不用Fake usage冒充实测Tokens。

## 回归风险
Runtime 350项通过。旧Module/Pinned Eval执行39项，出现9条失败记录（含同一方法内6个subTest），不能按39-9算通过测试数，也未改判PASS。
`eval/phase5/scenarios.py:BenchmarkResponses`仅识别旧摘要请求，无Extractor响应/with_options契约；`eval/pinned_constraints/gate.py:EvalClient`只提供固定摘要文本，现在无法满足Snapshot Extraction。分类为旧Fixture/API漂移；不修复冻结资产。原始日志保存在本轮runs目录。

## 复现
在Hexgent仓库执行：

```sh
.venv/bin/python -m eval.context_task_ab.offline_gate --regression
```

默认生成新run目录，禁止覆盖。没有真实模型执行参数。报告包含配置、资料与Production哈希、原始请求、恢复事件和失败日志。runs由现有Git规则忽略，核心Eval实现与Fixture保留追踪。

## 下一步边界
P0离线实现可审阅；不自动进入真实模型A/B。应先处理资料规模资格与旧Eval隔离：如果自然轨迹没有压缩机会，必须报告NO_COMPACTION_OPPORTUNITY，不能靠降低正式阈值、填充或机械回读补救。叙述性Markdown输出仍需人工语义复核，结构化决策由外部Oracle验证。
