"""上下文状态、Token 估算和主动压缩测试。"""

from types import SimpleNamespace
import unittest

from hexgent.context.compaction import compact_context_if_needed
from hexgent.utils.token_estimator import estimate_request_tokens
from hexgent.context.context_budget import ContextLimits
from hexgent.context.context import ConversationContext, UserQuery
from hexgent.events import (
    ContextCompactionFailedEvent,
    ContextCompactionFinishedEvent,
    ContextCompactionStartedEvent,
)


class FakeResponses:
    """记录上下文摘要请求并返回预设流式事件。"""

    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **arguments):
        """返回只包含 completed 的内部摘要事件流。"""
        self.calls.append(arguments)
        return iter(
            [SimpleNamespace(type="response.completed", response=self.response)]
        )


class FakeClient:
    """提供测试所需 Responses 接口。"""

    def __init__(self, response: SimpleNamespace) -> None:
        self.responses = FakeResponses(response)


def drain_generator(generator):
    """收集生成器事件并取得最终返回值。"""
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stopped:
            return events, stopped.value


class ContextCompactionTest(unittest.TestCase):
    """验证压缩只改变活动上下文，并遵守完整轮次边界。"""

    def test_compaction_preserves_transcript_and_recent_messages(self) -> None:
        """成功压缩应保留完整记录，并在活动上下文中留下摘要和近期消息。"""
        conversation = ConversationContext()
        conversation.append_message({"role": "user", "content": "较早的问题"})
        conversation.append_messages(
            [SimpleNamespace(type="message", text="较早的回答")],
            completes_round=True,
        )
        latest_message = {"role": "user", "content": "最新的问题"}
        conversation.append_message(latest_message)
        transcript_before = list(conversation.transcript)
        response = SimpleNamespace(
            output_text="用户先前询问了一个问题，Agent 已经完成回答。",
            usage=SimpleNamespace(input_tokens=20, output_tokens=8),
        )
        client = FakeClient(response)

        events, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                ContextLimits(
                    context_window_tokens=12,
                    reserved_output_tokens=1,
                    safety_margin_tokens=1,
                    recent_context_tokens=100,
                ),
            )
        )

        self.assertEqual(outcome.status, "compacted")
        self.assertIsInstance(events[0], ContextCompactionStartedEvent)
        self.assertIsInstance(events[1], ContextCompactionFinishedEvent)
        self.assertIsInstance(conversation.active_messages[0], UserQuery)
        self.assertEqual(conversation.active_messages[0].content, "较早的问题")
        self.assertIn("压缩摘要", conversation.active_messages[1]["content"])
        self.assertEqual(
            conversation.active_messages[-1].content,
            latest_message["content"],
        )
        self.assertEqual(conversation.transcript[: len(transcript_before)], transcript_before)
        self.assertEqual(conversation.transcript[-1]["type"], "context_compaction_boundary")
        self.assertEqual(outcome.input_tokens, 20)
        self.assertEqual(outcome.output_tokens, 8)

    def test_every_user_query_remains_verbatim_after_compaction(self) -> None:
        """历史压缩不得改写、删除或摘要任何用户 Query 原文。"""
        conversation = ConversationContext()
        first = conversation.append_user_query("第一条 Query，保留原始标点！")
        conversation.append_messages(
            [SimpleNamespace(type="message", text="第一轮回答" * 100)],
            completes_round=True,
        )
        second = conversation.append_user_query("第二条 Query 也必须保持不变。")
        client = FakeClient(SimpleNamespace(output_text="历史工作摘要", usage=None))

        _, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                ContextLimits(
                    context_window_tokens=10,
                    reserved_output_tokens=1,
                    safety_margin_tokens=1,
                    recent_context_tokens=100,
                ),
            )
        )

        self.assertEqual(outcome.status, "compacted")
        self.assertEqual(conversation.user_queries, (first, second))
        self.assertEqual(
            [
                message["content"]
                for message in conversation.model_messages()
                if isinstance(message, dict) and message.get("role") == "user"
            ],
            [first.content, second.content],
        )

    def test_tool_call_and_result_are_sent_to_summary_together(self) -> None:
        """同一完整轮次里的工具调用和结果不能被压缩切点拆开。"""
        conversation = ConversationContext()
        conversation.append_message({"role": "user", "content": "读取文件"})
        tool_call = SimpleNamespace(
            type="function_call",
            call_id="call-1",
            name="read_file",
            arguments='{"path":"a.py"}',
        )
        tool_result = {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "文件内容",
        }
        conversation.append_messages([tool_call])
        conversation.append_messages([tool_result], completes_round=True)
        conversation.append_message({"role": "user", "content": "继续总结"})
        client = FakeClient(
            SimpleNamespace(output_text="已读取文件。", usage=None)
        )

        _, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                ContextLimits(
                    context_window_tokens=10,
                    reserved_output_tokens=1,
                    safety_margin_tokens=1,
                    recent_context_tokens=100,
                ),
            )
        )

        summary_input = client.responses.calls[0]["input"]
        self.assertEqual(outcome.status, "compacted")
        self.assertIn(tool_call, summary_input)
        self.assertIn(tool_result, summary_input)

    def test_no_safe_boundary_reports_failure_without_changing_messages(self) -> None:
        """没有完整轮次边界时不得强行截断活动消息。"""
        conversation = ConversationContext()
        conversation.append_message({"role": "user", "content": "很长的新请求" * 20})
        active_before = list(conversation.active_messages)
        client = FakeClient(SimpleNamespace(output_text="不会调用", usage=None))

        events, outcome = drain_generator(
            compact_context_if_needed(
                conversation,
                client,
                "fake",
                "系统提示词",
                [],
                ContextLimits(
                    context_window_tokens=10,
                    reserved_output_tokens=1,
                    safety_margin_tokens=1,
                ),
            )
        )

        self.assertEqual(outcome.status, "blocked")
        self.assertIsInstance(events[0], ContextCompactionFailedEvent)
        self.assertEqual(conversation.active_messages, active_before)
        self.assertEqual(client.responses.calls, [])

    def test_request_estimation_includes_runtime_parts(self) -> None:
        """请求估算应同时包含指令、消息和工具定义。"""
        messages_only = estimate_request_tokens("", [{"role": "user", "content": "你好"}], [])
        complete_request = estimate_request_tokens(
            "额外系统指令",
            [{"role": "user", "content": "你好"}],
            [{"type": "function", "name": "read_file"}],
        )

        self.assertGreater(complete_request, messages_only)


if __name__ == "__main__":
    unittest.main()
