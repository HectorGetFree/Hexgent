# Workspace Memory — Real-model Evidence V1

## 范围

三组独立任务：project / preference / constraint。每组 A 自然请求长期记住，
B 在同一工作区的新进程、新 Session 中使用约定。共六次真实模型任务，由人工通过生产 CLI 执行。
prepare / analyze 绝不调用 Provider；离线测试不是模型能力证据。

不改变 Memory、ContextRecord、Compression、检索或权限实现。不做 ON/OFF、自动提取、
效率收益或 ContextRecord 评测。配置继承准备时的 hexgent.toml，仅本次评测明确 auto_approve；
生产硬拒绝仍有效。临时工作区不是 OS sandbox。

## 执行

在仓库根目录，先保证当前终端已 export OPENAI_API_KEY、OPENAI_BASE_URL、MODEL_ID。
隔离工作区不复制 .env；生产 dotenv 可能从源码所在仓库加载 .env 并覆盖 export，
按现有生产配置执行，并从 Provider 观测确认实际模型。报告核验 A/B 模型及 Provider 一致。

```sh
cd /Users/hector/dev/hexgent
export MEMORY_EVAL_OUTPUT="$PWD/eval/memory_hardening/runs/real-v1-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python -m eval.memory_hardening prepare --output-dir "$MEMORY_EVAL_OUTPUT"
cat "$MEMORY_EVAL_OUTPUT/commands.txt"
```

按 commands.txt 分组执行，不要整段当脚本运行。cat 显示提示后启动 CLI，将提示粘入并发送。
每个 Session 只发送一次指定任务，等终态后 exit。B 必须重新启动，不能 /restore、/continue。
不要在工作区里放提示 A、答案、Oracle 或真实用户文档。不要人为写入 Memory 补救失败。
工作区在系统临时目录，但不会自动删除；所有原始 Transcript / observation 必须保留到审核完成。

全部六次任务完成后，可自动关联 Session 并输出三组总报告：

```sh
cd /Users/hector/dev/hexgent
.venv/bin/python -m eval.memory_hardening analyze-all --output-dir "$MEMORY_EVAL_OUTPUT"
```

自动关联要求每组 A/B 各有唯一匹配 Session，不能在失败后偷偷选择一次成功尝试。
缺失或重复会标记 BLOCKED，并保持总分母为三组。若需逐组审计，可列出 Session：

```sh
cd /Users/hector/dev/hexgent
.venv/bin/python -c 'import json,os,pathlib; m=json.loads((pathlib.Path(os.environ["MEMORY_EVAL_OUTPUT"])/"manifest.json").read_text()); print("\n".join(str(p) for c in m["cases"].values() for p in sorted((pathlib.Path(c["workspace"])/".hexgent/sessions").glob("*"))))'
.venv/bin/python -m eval.memory_hardening analyze \
  --output-dir "$MEMORY_EVAL_OUTPUT" --case project \
  --session-a '/替换为/project/.hexgent/sessions/A目录' \
  --session-b '/替换为/project/.hexgent/sessions/B目录'
```

preference / constraint 使用各自 Session 路径和 --case。报告新增时间戳文件，不覆盖原始证据。
源码、配置或任务提示改变时必须新建评测目录，不复用旧 manifest。

## Oracle 与指标

1. **Persistence**：A durable remember success → 返回 memory_id → 正式 mem-*.md，
   source=explicit，项目名及要求的事实完整。不把模型口头“记住了”当成功。
2. **Injection**：B 第一条成功 MAIN_AGENT 请求的 memory_injected 凭证，
   ID 与持久化正文 SHA-256 一致。凭证在去重、预算裁剪后生成，于实际 SDK 调用前关联 span；
   不保存请求正文。失败/取消请求不算有效注入。缺观测为 BLOCKED，不能用 retrieval snapshot 代替。
3. **Behavior**：B terminal completed，最终 JSON 严格符合事先冻结的结构、值及顺序。
   B 无需工具；发生额外工具调用标记归因受污染，不静默当作自动召回成功。
4. **Freshness**：不同 Session ID、单次任务、准确提示、相同生产源码与配置。

实际注入 Recall = 注入的 gold IDs / 持久化 gold IDs；Precision = 注入 gold IDs / 注入 IDs。
未建立持久化 Gold 或无有效请求时 Recall=null；零注入 Precision=null，不填零冒充测量。
三组各一次的写入/注入/行为结果只能描述这三个场景，不能称为广泛 Recall@K 或泛化成功率。
无 OFF 对照，不能证明“没有 Memory 就不会成功”，也不能声称节省 Token、轮次或时间。
事实检查采用明确文本锚点，语义等价改写可能需人工审阅；不能为通过而修改冻结 Oracle。

## 观测边界

新增生产代码仅为非权威注入观测；不改变 Memory 策略或 Provider 参数。
memory_injected 证明客户端构造并提交了对应上下文，不证明 Provider 内部注意力或理解。
Provider 失败、Usage 缺失分别保留；估算 Token 不补填 Provider Token。
辅助 ContextRecord 调用仍按原生产语义执行，费用计入观测，但不作为本轮评测对象。

## 离线验证

```sh
.venv/bin/python -m unittest eval.test_memory_hardening -v
.venv/bin/python -m unittest discover -s tests
git diff --check
```
