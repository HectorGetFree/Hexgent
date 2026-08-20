"""模型调用与工具执行的核心循环。"""

from typing import Any

from .client import get_client, get_model
from .prompt import SYSTEM_PROMPT, TOOLS
from hexgent.tool.tool_execution import execute_tool_call


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
        for tool_call in tool_calls:
            result = execute_tool_call(
                tool_call.name,
                tool_call.arguments,
                execution_notifier=lambda name: print(
                    f"\033[33m使用工具：{name}\033[0m"
                ),
            )

            results.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": result.output,
                }
            )

        messages.extend(results)
