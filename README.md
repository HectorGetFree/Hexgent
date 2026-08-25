# Hexgent

Hexgent 是一个基于 OpenAI Responses API 的中文命令行个人助理。它支持流式对话、受权限控制的本地工具调用，以及面向复杂任务的会话级执行计划（Plan）。

## 功能特性

- **交互式命令行对话**：支持多行输入，空行或 `/send` 发送消息；输入 `q` 或 `exit` 结束会话。
- **流式输出**：模型生成内容会实时显示在终端中。
- **本地工具调用**：可读取、写入、编辑和查找文件，或运行 Shell 命令。
- **权限边界**：工具调用会经过统一的参数校验与权限决策；访问工作区外文件、执行高风险命令时需要用户确认，硬拒绝规则不能绕过。
- **执行计划**：面对复杂任务，Agent 会创建并持续更新 Plan，保留稳定目标、步骤状态、版本号与阻塞原因。
- **运行预算**：限制单次 Agent Run 的模型轮次、工具次数、Token 和总耗时；达到限制后会进入一次无工具的收尾总结。
- **重复调用控制**：文件读取使用内容 Hash 避免重复正文，安全的查询工具可在单次 Agent Run 内复用相同参数的成功结果。

## 环境要求

- Python 3.10 或更高版本
- 可访问 OpenAI 兼容 Responses API 的服务

## 安装

使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync
```

或使用 pip：

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install .
```

## 配置

普通运行参数保存在项目根目录的 `hexgent.toml`，包括：

- Agent Loop 的模型轮次、工具次数、累计 Token 和总时间预算；
- 活动上下文窗口、压缩阈值和摘要预算；
- 单个及批次工具结果的内联、外置与预览预算。

配置文件不存在或字段缺失时使用代码默认值。密钥不应写入 TOML，仍然放在 `.env` 中：

在项目根目录创建 `.env` 文件：

```dotenv
OPENAI_API_KEY=你的_API_密钥
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL_ID=你的模型_ID
```

`OPENAI_BASE_URL` 与 `MODEL_ID` 均从环境变量读取，因此也可以配置为兼容 OpenAI API 的其他服务。

> 请勿提交包含真实密钥的 `.env` 文件。

## 启动

在项目根目录执行：

```bash
python -m hexgent
```

指定其他运行配置：

```bash
uv run python -m hexgent --config hexgent.compaction-test.toml
```

该测试配置会在约 20,000 Token 时主动压缩上下文，并关闭跨轮次累计预算，便于观察完整压缩流程。单次模型输出上限和具体工具自身超时仍然有效。

也可使用兼容入口：

```bash
python main.py
```

启动后按提示输入内容。多行消息输入完成后，按一次空行或输入 `/send` 提交。

## 工具与权限

Hexgent 为模型提供以下工具：

| 工具 | 作用 |
| --- | --- |
| `bash` | 执行 Shell 命令 |
| `read_file` | 读取 UTF-8 文件 |
| `write_file` | 写入并覆盖 UTF-8 文件 |
| `edit_file` | 对唯一匹配的文本进行替换 |
| `glob` | 按 Glob 模式查找文件或目录 |
| `create_plan` | 为复杂目标创建初始执行计划 |
| `update_plan` | 更新计划进度或进行重规划 |

所有工具调用均经过统一执行管线：工具存在性检查 → JSON 与 Schema 校验 → 权限决策 →（必要时）用户确认 → 执行 → 结果回传模型。

工作区根目录会从当前目录向上查找最近的 `pyproject.toml` 或 `.git` 标识。相对路径以该工作区为基准；对工作区外路径的访问会请求确认。

## Plan 执行模型

当任务包含多个步骤时，Agent 会维护一个会话级 Plan：

- `objective` 是稳定目标，创建后不能修改；
- 每个步骤具有独立 `step_id` 和 `pending`、`in_progress`、`completed` 状态；
- 正在执行的 Plan 必须恰好有一个 `in_progress` 步骤；
- 已完成步骤不能被删除、回退或改写；
- 计划发生增删、替换或重排时需要记录重规划原因；
- 无法继续时，Plan 会标记为 `blocked` 并记录具体原因。

Plan 目前仅保存在进程内存中，程序退出后不会恢复。

## 项目结构

```text
.
├── hexgent/
│   ├── agent_engine.py  # CLI 入口与事件渲染
│   ├── client.py        # OpenAI 客户端与模型配置
│   ├── config.py        # TOML 普通运行配置加载与校验
│   ├── context.py       # 系统提示词与 Plan 上下文
│   ├── plan.py          # Plan 数据模型与内存存储
│   ├── prompt.py        # 系统提示词和工具 Schema
│   ├── session/         # Session 生命周期、Transcript 与会话工件
│   ├── terminal_io.py   # 终端输入与权限确认
│   ├── workspace.py     # 工作区根目录发现
│   ├── loop/            # Agent 循环与运行限制
│   └── tool/            # 工具注册、校验、权限和执行管线
├── tests/               # 单元测试
├── docs/                # 设计文档
├── hexgent.toml         # 默认运行配置
├── hexgent.compaction-test.toml # 上下文压缩专项测试配置
├── pyproject.toml
└── main.py
```

当前工具、权限、Agent Loop、Context、缓存、大结果和 Transcript 的统一设计说明，请参阅 [docs/核心机制设计.md](docs/核心机制设计.md)。

## 运行测试

```bash
python -m unittest discover -s tests
```

## 当前限制

- 权限规则中的 Shell 风险判断主要依赖字符串匹配，不能完整理解所有 Shell 语法。
- Plan 仅存储于内存，不支持跨进程恢复。
- 当前交互界面为本地终端；权限确认通过终端输入完成。

## 许可证

当前仓库未提供许可证声明。如需发布或复用，请先补充合适的许可证文件。
