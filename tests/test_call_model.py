"""验证模型流协议中的无副作用恢复边界。"""

from types import SimpleNamespace
import unittest

from hexgent.loop.call_model import call_model


def _drain(generator):
    """消费事件流并取得生成器的最终返回值。"""
    while True:
        try:
            next(generator)
        except StopIteration as completed:
            return completed.value


class CallModelTest(unittest.TestCase):
    """只允许对没有任何可见输出的残缺空流进行一次重试。"""

    def test_retries_empty_stream_once_before_returning_completed_response(self):
        """首次空流没有工具或文本，因此重试不会重复副作用或界面内容。"""
        response = SimpleNamespace(output_text="完成")
        streams = [iter(()), iter([SimpleNamespace(type="response.completed", response=response)])]
        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **_kwargs: streams.pop(0))
        )

        actual = _drain(call_model(client, model="test", input=[]))

        self.assertIs(actual, response)
        self.assertEqual(streams, [])


if __name__ == "__main__":
    unittest.main()
