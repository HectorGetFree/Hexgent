"""Core model and tool execution loop."""

import json
from typing import Any

from .client import get_client, get_model
from .prompt import BASH_TOOLS, SYSTEM_PROMPT
from .tools import run_bash


def agent_loop(messages: list[Any]) -> str:
    """Run model turns until the model returns without a tool call."""
    client = get_client()

    while True:
        response = client.responses.create(
            model=get_model(),
            instructions=SYSTEM_PROMPT,
            input=messages,
            tools=BASH_TOOLS,
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
            arguments = json.loads(call.arguments)
            command = arguments["command"]

            print(f"\033[33m$ {command}\033[0m")
            output = run_bash(command)
            print(output[:200])

            results.append(
                {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": output,
                }
            )

        messages.extend(results)
