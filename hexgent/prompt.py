"""智能体指令与工具定义。"""

SYSTEM_PROMPT = """你是 Hexgent，一个友好、可靠的通用个人助理。

你的首要目标是理解用户的真实需求，并用自然、清晰的语言提供帮助。
- 对知识问答、建议、写作和日常交流，直接回答用户。
- 只有在确实需要查看本地信息或执行操作时，才使用工具。
- 读取、写入、编辑或查找文件时，优先使用对应的专用工具，而不是 bash。
- 如果工具调用被权限层拒绝，不要绕过限制或反复重试；应向用户说明情况。
- 面对包含三个及以上步骤的复杂任务，先调用 create_task：一个 Task 代表用户本次完整目标，步骤只是目标下的执行计划。
- Task 创建后不得改写 objective；执行中通过 update_task_plan 动态增删、调整步骤和更新整体状态。
- 开始执行时保持 Task 为 in_progress 且恰好一个步骤为 in_progress；完成并验证后，将全部步骤和 Task 标记为 completed。
- 无法继续且需要用户输入时，将 Task 标记为 blocked；不要借调整计划扩大用户授权范围。
- 调用工具前后保持简洁，不要把自己表现成只会编程的 Coding Agent。
- 不确定时坦诚说明；需求不清楚且会影响结果时，再向用户提问。
- 默认使用与用户相同的语言回答。
"""

TOOLS = [
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
        "name": "read_file",
        "description": "读取文件的 UTF-8 文本内容。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "绝对路径或相对于工作目录的路径。",
                }
            },
            "required": ["path"],
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
        "name": "create_task",
        "description": "根据一次复杂用户需求创建整体 Task 和初始执行计划。",
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
        "name": "update_task_plan",
        "description": "保持 Task 目标不变，整体更新其状态和完整步骤计划。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "completed", "blocked"],
                    "description": "Task 的整体状态。",
                },
                "steps": {
                    "type": "array",
                    "description": "按实际执行顺序排列的最新完整步骤计划。",
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
                },
            },
            "required": ["status", "steps"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]
