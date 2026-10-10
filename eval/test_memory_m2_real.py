"""验收器离线契约验证，不访问真实 Provider。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from hexgent.config import HexgentConfig
from hexgent.memory import MemoryStore
from eval.support.agent_contracts import FakeModel, text_response, tool_call
from eval.memory_m2_real import CASES, RequestRecorder, execute_case


class MemoryM2RealTest(TestCase):
    def test_five_scenarios_use_native_tools_and_formal_continue(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            def factory(case, stage):
                if stage == "WRITE":
                    args = dict(content=case["fact"], category=case["category"], keywords=[case["entity"]])
                    responses = [text_response(output=[tool_call("write", "remember", json.dumps(args))]), text_response("已记住")]
                elif stage == "MUTATE":
                    vault = root / case["id"] / ("scope" if case.get("resume") else "vault")
                    identity = MemoryStore(vault).items[0].memory_id
                    args = dict(memory_id=identity)
                    if case["operation"] == "supersede_memory":
                        args = dict(old_memory_id=identity, content=case["new_fact"], category="project", keywords=[case["entity"]])
                    responses = [text_response(output=[tool_call("mutate", case["operation"], json.dumps(args))]), text_response("已执行")]
                elif stage == "PAUSE":
                    responses = [text_response(output=[tool_call("probe", "glob", '{"pattern":"*.m2-nothing"}')]), text_response("unused")]
                else:
                    responses = [text_response(json.dumps(dict(value=case["expected"]), ensure_ascii=False))]
                for response in responses:
                    response.usage = dict(input_tokens=1, output_tokens=1)
                return SimpleNamespace(responses=FakeModel(responses), base_url="https://offline.invalid")
            for case in CASES:
                with self.subTest(case=case["id"]):
                    result = execute_case(root, HexgentConfig(), case, factory)
                    self.assertEqual(result["status"], "PASS", result)

    def test_recorder_forwards_options_arguments_and_error(self):
        calls = []
        class Native:
            base_url = "offline"
            def __init__(self):
                self.responses = SimpleNamespace(create=self.create)
            def with_options(self, **options):
                calls.append(options)
                return self
            def create(self, **request):
                calls.append(request)
                raise TimeoutError("synthetic")
        records = []
        recorder = RequestRecorder(Native(), records).with_options(timeout=3)
        with self.assertRaises(TimeoutError):
            recorder.responses.create(model="offline", input=[], stream=True)
        self.assertEqual(calls, [dict(timeout=3), dict(model="offline", input=[], stream=True)])
        self.assertEqual(records[0]["request"], calls[1])
