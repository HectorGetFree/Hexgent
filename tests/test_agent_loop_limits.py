"""Agent Loop 执行限制测试。"""

from importlib import import_module
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hexgent.loop_control import LoopLimits
from hexgent.task import InMemoryTaskStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import ToolExecutionResult


agent_loop_module = import_module("hexgent.agent_loop")


class FakeResponses:
    """按顺序返回预设模型响应。"""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **arguments):
        """记录请求参数并返回下一个响应。"""
        self.calls.append(arguments)
        return self.responses.pop(0)


class FakeClient:
    """只提供测试所需 Responses 接口的客户端。"""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = FakeResponses(responses)


def make_response(
    output,
    text: str = "",
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> SimpleNamespace:
    """创建包含固定 Token 用量的测试响应。"""
    return SimpleNamespace(
        output=output,
        output_text=text,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


def make_tool_call(call_id: str) -> SimpleNamespace:
    """创建一个读取文件的模型工具调用。"""
    return SimpleNamespace(
        type="function_call",
        call_id=call_id,
        name="read_file",
        arguments='{"path":"a"}',
    )


class AgentLoopLimitsTest(unittest.TestCase):
    """验证循环限制和工具结果配对。"""

    def setUp(self) -> None:
        """为每个用例创建独立工具上下文。"""
        self.context = ToolExecutionContext(InMemoryTaskStore())

    def test_round_limit_reserves_one_finalization_response(self) -> None:
        """正常轮次耗尽后应额外生成一次不带工具的收尾响应。"""
        client = FakeClient(
            [make_response([make_tool_call("c1")]), make_response([], "已收尾")]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(
                agent_loop_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            result = agent_loop_module.agent_loop(
                [], self.context, LoopLimits(max_model_rounds=1)
            )

        self.assertEqual(result.stop_reason, "max_model_rounds")
        self.assertEqual(result.output_text, "已收尾")
        self.assertEqual(len(client.responses.calls), 2)
        self.assertIn("tools", client.responses.calls[0])
        self.assertNotIn("tools", client.responses.calls[1])

    def test_every_tool_call_gets_a_result_after_tool_limit(self) -> None:
        """批次中额度耗尽后，所有未执行调用仍应获得配对结果。"""
        client = FakeClient(
            [
                make_response(
                    [make_tool_call("c1"), make_tool_call("c2"), make_tool_call("c3")]
                ),
                make_response([], "工具额度收尾"),
            ]
        )
        messages = []
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(
                agent_loop_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            result = agent_loop_module.agent_loop(
                messages, self.context, LoopLimits(max_tool_calls=1)
            )

        outputs = [
            item
            for item in messages
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        ]
        self.assertEqual([item["call_id"] for item in outputs], ["c1", "c2", "c3"])
        self.assertEqual(outputs[0]["output"], "ok")
        self.assertTrue(all("未执行" in item["output"] for item in outputs[1:]))
        self.assertEqual(result.stop_reason, "max_tool_calls")

    def test_time_limit_returns_elapsed_time(self) -> None:
        """时间额度耗尽时应直接收尾并返回实际用时。"""
        client = FakeClient([make_response([], "时间收尾")])
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
        ):
            result = agent_loop_module.agent_loop(
                [], self.context, LoopLimits(max_elapsed_seconds=0)
            )

        self.assertEqual(result.stop_reason, "max_elapsed_seconds")
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        self.assertNotIn("tools", client.responses.calls[0])

    def test_total_token_limit_enters_finalization(self) -> None:
        """累计 Token 达到上限后应在下一轮进入收尾阶段。"""
        client = FakeClient(
            [
                make_response(
                    [make_tool_call("c1")],
                    input_tokens=40,
                    output_tokens=20,
                ),
                make_response([], "Token 收尾"),
            ]
        )
        with (
            patch.object(agent_loop_module, "get_client", return_value=client),
            patch.object(agent_loop_module, "get_model", return_value="fake"),
            patch.object(
                agent_loop_module,
                "execute_tool_call",
                return_value=ToolExecutionResult("success", "ok", False),
            ),
        ):
            result = agent_loop_module.agent_loop(
                [], self.context, LoopLimits(max_total_tokens=50)
            )

        self.assertEqual(result.stop_reason, "max_total_tokens")
        self.assertEqual(result.output_text, "Token 收尾")
        self.assertEqual(len(client.responses.calls), 2)


if __name__ == "__main__":
    unittest.main()
