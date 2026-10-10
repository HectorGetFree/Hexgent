# M2：长期 Memory 生命周期契约

## 工具与作用域

- `supersede_memory(old_memory_id, content, category, keywords)`：显式替代。
- `invalidate_memory(memory_id)`：逻辑失效，保留正文。
- `delete_memory(memory_id)`：删除正文，保留 ID 墓碑。

全部经过 Production Tool Pipeline，仅使用绑定的 `ToolExecutionContext.memory_store`，
不接受目录或 Scope 参数。默认 ASK，现有 auto_approve 显式模式仍按其原有语义处理。
CLI 和 Service 无新增管理端点，模型调用同一注册工具及 MemoryStore 方法。

## 权威与兼容

Memory ID 仍由类别和正规化正文生成。相同有效正文合并元数据；失效/删除正文不得由 remember 复活。
ACTIVE、SUPERSEDED、INVALIDATED 为三个条目状态，删除仅保留墓碑。
替代必须明确指定旧 ID；不做自动冲突识别，不把旧候选静默转成替代者。
链式替代支持 A→B→C；自替代、环、非 ACTIVE 节点替代拒绝。
重复相同替代仅在目标仍有效时返回原结果；目标后来撤销则返回冲突。
失效及删除幂等；未知 ID 拒绝，不级联撤销后继事实。

`lifecycle.json` 是新的单文件事务权威，包含条目正文、状态、revision 和删除墓碑。
`items/mem-*.md`、`MEMORY.md` 是正文/索引投影，不决定有效性。
这是为本地多条目原子提交所需的权威迁移，不再声称 items 是唯一权威。
旧条目在首次受控访问时锁内迁移为 ACTIVE；合法 ID 不变。
早期单文件中与正文不匹配的 ID 按现有内容 ID 规则重新生成。
清单建立后，孤儿文件不会被自动导入。人工编辑投影文件不是受支持的事实更新方式。

## 原子性及恢复

同一目录共享 flock 跨进程排他锁，每次操作重新读取权威清单。
新正文先持久化，随后原子替换/fsync 清单是逻辑提交点，索引是可再生投影。
未提交的正文文件不可召回；提交后索引失败不使旧事实复活。
删除先提交墓碑，再清理正文；清理失败工具返回需核验错误，重试继续清理。
已有 Store 实例的读取刷新权威，不依赖旧 `_items`。
不保证断电实验结果、网络文件系统锁语义、绕过 API 的外部编辑或旧版本进程混用。

## 撤销复核

1. Retrieval 默认只读取 ACTIVE；Retriever 也过滤显式非 ACTIVE 条目。
2. Context Builder 依据当前权威 ID/正文复核已召回 Memory。
3. 正式 Restore→Continue 复核 Checkpoint 候选，历史快照不作为权威。
4. 主请求每次实际 `responses.create` 前（包括重试）再次过滤结构化 Memory 区块。
5. 注入凭证依据最终请求生成，复核与请求启动共用 Scope 锁；不持锁消费完整流。
6. 精确 remember 工具缓存命中前复核，不能复用已撤销的旧成功结果。

无 Memory 区块的辅助请求不会新增长期 Memory 注入。
当前 Compaction 输入为活动历史消息，Extraction 输入为 durable Transcript：其中已存在的旧事实属于历史范围。
请求启动/SDK create 返回期间可能暂时阻塞撤销；SDK 内部重试不另作模型观察，锁覆盖 create 整段。
已启动的请求不被撤回，后续新请求不能新增注入已提交撤销的 Memory。
不清除历史 Transcript、remember 工具结果、模型文本、摘要、ContextRecord 中已有的事实；不保证模型全量遗忘。

## 验收

`tests/test_memory_lifecycle.py` 使用真实本地 Store、Production Pipeline、FakeModel、正式 Checkpoint/Continue。
覆盖单次/链式替代、幂等、重启、跨 Scope 拒绝、两个子进程竞争、提交前后失败、删除清理失败、
缓存撤销、已召回候选、最终请求及重试复核、CLI/Service 同链路。

保存合成权威文件、Transcript 和请求 Oracle 证据：

```sh
.venv/bin/python -m eval.memory_lifecycle \
  --output-dir "eval/memory_m2/runs/deterministic-$(date -u +%Y%m%dT%H%M%SZ)"
```

FakeModel usage 不是 Provider 实测成本。真实模型四项 Smoke 尚未执行，不能据此宣布 M2 全部 PASS。
