# Hexgent

Hexgent 是可独立运行的通用 Agent Runtime / Harness，提供 CLI 与 HTTP Service 两种入口。它围绕真实 Agent Loop 管理模型调用、工具治理、会话事实、上下文预算、长期 Memory 与安全恢复，并可通过受控 Capability 集成 [HexWorkspace](../hex-workspace)。

本地文件与 Shell 工具用于承载通用工作负载，不意味着项目定位为 Coding Agent。Runtime 不提供 OS 级沙箱，也不保证模型输出天然正确。

## 核心能力

### Agent Loop 与工具治理

- OpenAI Responses 兼容模型接入、流式输出、多轮工具调用与执行 Plan。
- 统一工具注册、JSON/Schema 校验、权限决策、确认、执行、重试与结果交付，严格关联 callId 与结果。
- 对声明为并行安全的独立只读调用支持并发；写操作和不安全调用按调度规则形成屏障。
- 精确调用去重、读取内容 Hash 复用、大结果落盘与有界预览；读取复用不代表减少实际文件 I/O。
- 模型轮次、工具次数、累计 Token、输出与时间预算，以及暂停、取消和收尾处理。
- 工具超时不等于底层线程一定停止；副作用未知调用不能依靠自动重试获得 exactly-once。

### Session、Transcript 与恢复

- JSONL Transcript 保存运行事实，派生 Checkpoint、Plan 投影与 Run Report；状态不再仅在内存中保存。
- 正式 Restore → Continue 复用原 Session 和可恢复 Run，不等于创建新消息重新开始。
- 已持久化完成结果可恢复到模型上下文；未闭合工具调用按未执行、安全查询、未知副作用等事实生成保守恢复结果。
- 未知副作用需要核验，不自动承诺重新执行；只有满足当前恢复契约的状态才允许继续。
- Session 写入所有权与序号校验防止并发写坏事实日志。Checkpoint 是可再生加速产物，不是独立事实权威。

### 上下文、Plan 与显式约束

- 确定性 Context Builder 组装 System、Pinned Constraints、Plan、Skill、ContextRecord、Memory；历史与工具结果作为独立消息进入请求。
- 完整请求预算包含 instructions、input、tools 与输出预留；拒绝不能容纳的请求，估算 Token 不等于 Provider Usage。
- 在完整轮次边界执行 Compaction，保留近期上下文，通过 Key Fact Snapshot + Summary 校验后原子提交 Transcript，再发布内存与 Checkpoint。
- Snapshot Extractor 默认使用已集成的 v2 Prompt。结构和来源校验不保证语义事实绝对无损，辅助模型也产生额外费用与延迟。
- 用户通过 CLI 显式 Pin / Replace / Revoke Session 约束；生命周期事件可重放，ACTIVE 原文完整注入，超预算拒绝而非截断。
- Pinned 是提示保留机制，不替代工具权限；管理命令不作为模型工具开放。

### 跨 Session Memory 与 Skill

- 用户明确要求后，经 `remember` 写入本地长期 Memory；新 Session 在同一 Workspace 中按需检索、注入。
- 支持 `supersede_memory`、`invalidate_memory`、`delete_memory`；通过权威生命周期、墓碑与请求前复核阻止失效 Memory 再次注入。
- 撤销针对长期 Memory 注入渠道，不删除历史 Transcript、已发送请求或模型已有信息。
- Memory 当前以本地 Workspace 目录为作用域，不是按用户隔离的多租户存储；多身份共享目录需额外设计。
- Skill 支持目录发现、显式激活和受控创建。
- 当前仓库 TOML 关闭 ContextRecord 自动提取与召回；它与长期 Memory、Compaction Key Fact Snapshot 是不同机制。

### HexWorkspace 集成

通过 Runtime Command、事件流、权限与生命周期适配器对接 Java 服务。已注册的业务工具包括：

`search_workspace`、`list_folder`、`get_document`、`create_document`、`update_document`。

业务身份和范围由 Workspace 的权威 AgentRun 与 Capability Gateway 校验，不允许模型任意选择用户或 Workspace。后端普通移动/删除接口不等于已注册 Agent 工具。

## 安装与模型配置

要求 Python 3.10+ 和可用的 Responses 兼容模型服务。

```bash
uv sync
# 或：
python -m venv .venv
source .venv/bin/activate
pip install .
```

在本地 `.env` 配置，勿提交真实凭证：

```dotenv
OPENAI_API_KEY=你的密钥
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL_ID=你的模型ID
```

当前 Provider 加载逻辑会以 `.env` 覆盖同名环境变量；测试不同模型时确认实际生效配置，不要只依赖终端 export。Provider 兼容程度、可用容量、Usage 和错误行为需要独立验证。

## CLI

在希望作为 Workspace 的目录启动，或在仓库根目录：

```bash
python -m hexgent
# 指定配置：
python -m hexgent --config hexgent.toml
```

工作区根目录会向上寻找项目标识；相对文件路径以工作区为基准。多行输入以空行或 `/send` 提交，`exit` / `q` 退出。

| 命令 | 作用 |
| --- | --- |
| `/restore [session_id]` | 选择或恢复历史 Session |
| `/continue` | 正式继续满足恢复契约的暂停任务 |
| `/pin <原文>` | 显式固定当前 Session 约束 |
| `/replace-pin <id> <version> <新原文>` | 显式替代约束 |
| `/revoke-pin <id> <version>` | 撤销约束 |
| `/pins` | 查看约束身份、版本与状态 |

约束管理只允许安全 Run 边界，冲突会失败，不会隐式排队。Service 尚无对应的、完成最终用户—Session 授权的 Pinned 管理入口。

## Service

```bash
python -m hexgent.adapters.web \
  --host 127.0.0.1 --port 8000 \
  --workdir /absolute/path/to/trusted-workspace
```

可选 `--config /absolute/path/to/hexgent.toml`。

- `/api/sessions`、`/api/sessions/{id}/runs`：本地 Web Session/Run API。
- `/api/runs/{id}/events`：事件流；另有暂停、取消、引导和权限操作。
- `/internal/v1/runtime/commands`：Workspace 集成命令；内部路由使用 Integration Token。

默认仅监听回环地址。本地 Web API 不应作为已具备完整用户认证和 OS 隔离的公网服务直接暴露；CORS 不是身份认证。

集成配置：

| 变量 | 用途 |
| --- | --- |
| `HEXGENT_WORKSPACE_URL` | Workspace 后端地址，默认 8098 |
| `HEXGENT_WORKSPACE_CAPABILITY_TOKEN` | 业务工具调用认证 |
| `HEXGENT_WORKSPACE_INTEGRATION_TOKEN` | Runtime 内部命令认证 |
| `HEXGENT_WORKSPACE_LIFECYCLE_URL` / `HEXGENT_WORKSPACE_LIFECYCLE_TOKEN` | 可选生命周期回调 |
| `HEXGENT_WORKSPACE_PERMISSION_URL` | 可选权限桥接，使用 Lifecycle Token |

两端配置须匹配。内部凭证不等于用户授权，部署前审阅业务身份、共享工作目录及网络边界。

## 配置与安全默认值

`hexgent.toml` 管理 Loop、Context、工具结果、并行和权限策略。**当前检出的配置是本地调试取向：**

- `[tool_parallel] enabled=false`、`max_workers=8`：支持并发，但当前默认配置没有启用。
- `[permission] default_mode="auto_approve"`：自动批准非硬拒绝操作，不适合不可信任务。
- `[context_extraction]` 与 `[context_retrieval]` 均为 `enabled=false`。

需要保留人工确认时使用：

```toml
[permission]
default_mode = "default"

[tool_parallel]
enabled = true
max_workers = 8
batch_timeout_seconds = 60.0
```

这只是配置示例，不代表线程数越多越快。硬拒绝规则仍有效，但文件权限与 Shell 风险规则不是 OS 沙箱。Workspace 集成中的自动批准还受适配器额外开关约束，不与 CLI 配置混为同一契约。

## 持久化目录

```text
<workspace>/.hexgent/
├─ sessions/<session_id>/
│  ├─ transcript.jsonl       权威运行事件
│  ├─ tool-results/          外置工具结果
│  └─ reports/               派生运行报告；其他恢复产物由 Store 管理
└─ memory/
   ├─ lifecycle.json         迁移后生命周期权威状态
   ├─ items/                 条目正文
   └─ MEMORY.md              人类可读投影，不是有效性权威
```

不要提交包含用户内容、工具输出或敏感请求的 `.hexgent` 目录；Checkpoint、结果与 Transcript 的一致性应通过正式 Store/Restore 路径维护。

## 源码与评测

```text
hexgent/agent_engine.py Engine 与运行 API
hexgent/loop/          Agent Loop、模型轮次与状态转换
hexgent/tool/          注册、校验、权限、调度与结果治理
hexgent/context/       Builder、预算、Compaction 与 Key Fact Snapshot
hexgent/session/       Transcript、Checkpoint、恢复、所有权与 Pinned
hexgent/memory/        长期 Memory 及 ContextRecord 相关机制
hexgent/plan/          Plan 状态
hexgent/skill/         Skill 生命周期
hexgent/adapters/      CLI、Web 与 Workspace 集成入口
hexgent/integration/   Command、Capability、回调与权限协议
tests/                Runtime 离线回归
eval/                 模块、任务与真实模型实验资产
```

```bash
python -m unittest discover -s tests
```

不要将 FakeModel 契约测试当作真实模型成功率。真实 Provider Eval 需要单独授权、预算和 Artifact；历史失败保留，不以重跑覆盖。评测输出和部分 Fixture 被 Git 忽略，克隆代码不等于拥有完整复现证据。

设计说明：[核心机制](docs/核心机制设计.md)、[工具系统](docs/工具系统设计.md)、[上下文管理](docs/上下文管理设计.md)、[Memory](docs/Memory设计.md)、[运行观测](docs/runtime-observation.md)。


