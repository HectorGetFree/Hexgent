"""模型调用与工具执行的核心循环。"""

import json
from typing import Any

from .client import get_client, get_model
from .prompt import SYSTEM_PROMPT, TOOLS
from .tool_registry import execute_tool


def agent_loop(messages: list[Any]) -> str:
    """持续执行模型轮次，直到模型不再请求调用工具。"""
    client = get_client()

    while True:
        response = client.responses.create(
            model=get_model(),
            instructions=SYSTEM_PROMPT,
            input=messages,
            tools=TOOLS,
            max_output_tokens=8000,
        )
        messages.extend(response.output)

        tool_calls = [
            item for item in response.output if item.type == "function_call"
        ]
        if not tool_calls:
            return response.output_text

        results = []
        for call in tool_calls:
            try:
                arguments = json.loads(call.arguments)
            except json.JSONDecodeError as error:
                output = f"错误：工具参数不是有效的 JSON：{error}"
            else:
                print(f"\033[33m使用工具：{call.name}\033[0m")
                output = execute_tool(call.name, arguments)

            results.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": output,
                }
            )

        messages.extend(results)
