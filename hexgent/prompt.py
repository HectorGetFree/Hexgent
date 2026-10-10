"""智能体指令与工具定义。"""

SYSTEM_PROMPT = """你是 Hexgent，一个友好、可靠的通用个人助理。

你的首要目标是理解用户的真实需求，并用自然、清晰的语言提供帮助。
- 对知识问答、建议、写作和日常交流，直接回答用户。
- 只有在确实需要查看本地信息或执行操作时，才使用工具。
- 读取、写入、编辑或查找文件时，优先使用对应的专用工具，而不是 bash。
- 普通文件默认完整读取：调用 read_file 时将 start_line 和 line_count 设为 null。只有读取已知大文件、持久化工具结果或继续上一段内容时才指定行范围。
- 浏览目录直接子项时优先使用 list_directory；truncated 为 true 时表示列表不完整，可缩小目录范围或使用 glob 查找。
- 如果工具调用被权限层拒绝，不要绕过限制或反复重试；应向用户说明情况。
- 面对包含三个及以上步骤的复杂目标，先调用 create_plan 创建并开始跟踪执行计划。
- Plan 创建后不得改写 objective；获得新信息后应反思原计划是否仍然合理。步骤正常推进时只更新状态并将 reason 设为 null；仅在增删、替换或重排步骤时提供重规划 reason。
- 重规划时保留已有步骤的 step_id；新增步骤的 step_id 使用 null 并交由系统生成。不得删除、回退或改写已经完成的步骤。
- 开始执行时保持 Plan 为 in_progress 且恰好一个步骤为 in_progress；完成并验证后，将全部步骤和 Plan 标记为 completed。
- 无法继续且需要用户输入时，将 Plan 标记为 blocked；不要借调整计划扩大用户授权范围。
- 调用工具前后保持简洁，不要把自己表现成只会编程的 Coding Agent。
- 不确定时坦诚说明；需求不清楚且会影响结果时，再向用户提问。
- 默认使用与用户相同的语言回答。
- 只有用户明确要求“记住”、要求以后持续遵守某项偏好或约束时，才调用 remember 保存长期记忆；不要把一次任务的临时要求当作长期记忆。
- 当 available-skills 中存在与用户目标高度匹配的项目 Skill 时，可以调用 activate_skill；激活后必须遵守该 Skill 的完整说明和工具限制。
- 只有用户明确要求将流程沉淀为可复用 Skill 时，才调用 create_skill；创建后不要隐式激活，应在下一轮根据需要调用 activate_skill。
"""

TOOLS = [
    {
        "type": "function",
        "name": "create_document",
        "description": (
            "在当前授权 Workspace 的指定文件夹中创建 Markdown 文档。"
            "成功时会返回正式创建的 documentId 和精确 versionId。"
            "不要传入 userId、workspaceId、runId、callId 或其他内部身份字段。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "parentFolderId": {"type": "integer", "description": "父文件夹 ID。"},
                "title": {"type": "string", "description": "文档标题。"},
                "content": {"type": "string", "description": "完整的 Markdown 文本。"},
            },
            "required": ["parentFolderId", "title", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "update_document",
        "description": (
            "修改已有 Document，并基于精确的 expectedVersionId 创建新版本。"
            "expectedVersionId 必须来自 get_document 或 list_folder；版本过期时会返回 VERSION_CONFLICT，"
            "不会自动覆盖或合并最新版本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documentId": {"type": "integer", "description": "目标文档 ID。"},
                "expectedVersionId": {
                    "type": "integer",
                    "description": "本次修改基于的精确 DocumentVersion ID。",
                },
                "content": {"type": "string", "description": "完整的新 Markdown 文本。"},
            },
            "required": ["documentId", "expectedVersionId", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_folder",
        "description": (
            "List the direct children of a Workspace folder. Omit folderId to list the Workspace root. "
            "Use a returned folder id to browse that folder, or use a returned documentId and versionId "
            "with get_document. This tool is not recursive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "folderId": {"type": ["integer", "null"], "description": "目录 ID；省略表示 Workspace 根目录。"},
                "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 200, "description": "最多返回数量。"},
            },
            "required": ["folderId", "limit"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "get_document",
        "description": (
            "Use this tool when you already know the exact documentId and versionId "
            "and need to read more of that specific document version."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "documentId": {"type": "integer", "description": "文档 ID。"},
                "versionId": {"type": "integer", "description": "精确的文档版本号。"},
                "startLine": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": "1-based 起始行号。",
                },
                "lineCount": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 500,
                    "description": "读取行数。",
                },
            },
            "required": ["documentId", "versionId", "startLine", "lineCount"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "search_workspace",
        "description": "Search the current authorized Workspace knowledge base for information relevant to a query.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的知识问题或关键词。"},
                "topK": {"type": ["integer", "null"], "minimum": 1, "maximum": 20,
                         "description": "最多返回的知识片段数量；不传则使用默认值。"},
            },
            "required": ["query", "topK"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "workspace_capability",
        "description": "调用 Workspace 提供的受控 Capability。",
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {"type": "string"},
                "arguments": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string"},
                        "userId": {"type": "string"},
                        "workspaceId": {"type": "string"},
                    },
                    "required": ["message", "userId", "workspaceId"],
                    "additionalProperties": False,
                },
            },
            "required": ["capability", "arguments"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "bash",
        "description": "运行 Shell 命令并返回输出。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "需要执行的 Shell 命令。",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "list_directory",
        "description": "列出目录直接子项，不递归，区分文件、目录和符号链接。截断时仅返回部分条目。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "绝对路径或工作区相对路径。"},
                "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000, "description": "最多返回条目数，通常使用 200。"},
                "include_hidden": {"type": "boolean", "description": "是否包含点开头的隐藏项，通常为 false。"},
            },
            "required": ["path", "max_entries", "include_hidden"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "读取文件的 UTF-8 文本内容。普通文件必须将 start_line 和 "
            "line_count 设为 null 以完整读取；只有已知大文件、持久化工具结果或 "
            "继续读取上一段内容时才指定行范围。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "绝对路径或相对于工作目录的路径。",
                },
                "start_line": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "description": (
                        "分段读取的起始行号，从 1 开始；普通文件默认传 null。"
                    ),
                },
                "line_count": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": 500,
                    "description": (
                        "分段读取的行数，最多 500 行；普通文件默认传 null。"
                    ),
                },
            },
            "required": ["path", "start_line", "line_count"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "write_file",
        "description": "将 UTF-8 文本写入文件，并覆盖文件原有内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "绝对路径或相对于工作目录的路径。",
                },
                "content": {
                    "type": "string",
                    "description": "需要写入文件的完整内容。",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": "替换 UTF-8 文件中唯一出现的一段文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "绝对路径或相对于工作目录的路径。",
                },
                "old_text": {
                    "type": "string",
                    "description": "需要替换的原文本，必须在文件中仅出现一次。",
                },
                "new_text": {
                    "type": "string",
                    "description": "用于替换的新文本。",
                },
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "glob",
        "description": "按照 Glob 模式查找匹配的文件和目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob 匹配模式，例如 '**/*.py'。",
                }
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_plan",
        "description": "根据一次复杂用户目标创建并开始跟踪初始执行计划。",
        "parameters": {
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "从用户需求提炼出的稳定整体目标，创建后不可改写。",
                },
                "steps": {
                    "type": "array",
                    "description": "按实际执行顺序排列的初始完整步骤计划。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "description": "完成整体目标所需的具体步骤。",
                            },
                            "process_message": {
                                "type": "string",
                                "description": "执行中展示的中文描述。",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "步骤当前状态。",
                            },
                        },
                        "required": ["content", "process_message", "status"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["objective", "steps"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "update_plan",
        "description": "根据最新执行结果更新进度或重规划，同时保留目标和已完成事实。",
        "parameters": {
            "type": "object",
            "properties": {
                "expected_revision": {
                    "type": "integer",
                    "description": "当前 Plan 的 revision，用于拒绝过期计划覆盖新计划。",
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked"],
                    "description": "Plan 的整体状态。",
                },
                "reason": {
                    "type": ["string", "null"],
                    "description": "计划结构改变时说明重规划原因；纯步骤状态更新时必须为 null。",
                },
                "blocked_reason": {
                    "type": ["string", "null"],
                    "description": "Plan 阻塞时的具体原因；非 blocked 状态必须为 null。",
                },
                "steps": {
                    "type": "array",
                    "description": "按实际执行顺序排列的最新完整步骤计划。",
                    "items": {
                        "type": "object",
                        "properties": {
                            "step_id": {
                                "type": ["string", "null"],
                                "description": "已有步骤使用原 step_id；新增步骤使用 null。",
                            },
                            "content": {
                                "type": "string",
                                "description": "完成整体目标所需的具体步骤。",
                            },
                            "process_message": {
                                "type": "string",
                                "description": "执行中展示的中文描述。",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "步骤当前状态。",
                            },
                        },
                        "required": [
                            "step_id",
                            "content",
                            "process_message",
                            "status"
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": [
                "expected_revision",
                "status",
                "reason",
                "blocked_reason",
                "steps"
            ],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "remember",
        "description": "保存用户明确要求长期记住的偏好、约束、项目知识或设计决策。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "可独立理解的简短长期记忆，不得记录本次任务的临时步骤。",
                },
                "category": {
                    "type": "string",
                    "enum": ["preference", "constraint", "project", "decision"],
                    "description": "记忆类别。",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "用于未来词法召回的少量关键词。",
                },
            },
            "required": ["content", "category", "keywords"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "activate_skill",
        "description": "激活一个项目级 Skill，使后续轮次遵守其工作流和工具限制。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "available-skills 中列出的 Skill 名称。",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "create_skill",
        "description": "将用户明确要求复用的工作流程创建为新的项目级 Skill。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "小写短横线格式的唯一 Skill 名称。",
                },
                "description": {
                    "type": "string",
                    "description": "供未来选择 Skill 时阅读的简短用途说明。",
                },
                "allowed_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "该工作流实际需要的最小工具白名单；为空数组表示不额外收紧。",
                },
                "instructions": {
                    "type": "string",
                    "description": "完整、可复用的中文工作流说明，不包含 YAML frontmatter。",
                },
            },
            "required": ["name", "description", "allowed_tools", "instructions"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]
