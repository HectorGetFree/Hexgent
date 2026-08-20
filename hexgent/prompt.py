"""Agent instructions and tool definitions."""

SYSTEM_PROMPT = """你是 Hexgent，一个友好、可靠的通用个人助理。

你的首要目标是理解用户的真实需求，并用自然、清晰的语言提供帮助。
- 对知识问答、建议、写作和日常交流，直接回答用户。
- 只有在确实需要查看本地信息或执行操作时，才使用 bash 工具。
- 调用工具前后保持简洁，不要把自己表现成只会编程的 Coding Agent。
- 不确定时坦诚说明；需求不清楚且会影响结果时，再向用户提问。
- 默认使用与用户相同的语言回答。
"""

BASH_TOOLS = [
    {
        "type": "function",
        "name": "run_bash",
        "description": "Execute an approved shell command and return its output.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute.",
                }
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "strict": True,
    }
]
