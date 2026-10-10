"""通过正式模型协议入口验证成功、失败及辅助请求观测。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from hexgent.observation import RunObservation, current_observation
from hexgent.loop.call_model import call_model, ModelCallError


class ProviderObservationTests(unittest.TestCase):
    def test_real_protocol_path_records_usage_and_failure_without_proxy(self):
        with TemporaryDirectory() as directory:
            sink = RunObservation(SimpleNamespace(session_dir=Path(directory)))
            response = SimpleNamespace(id="response-1", usage={"input_tokens": 10, "output_tokens": 2})
            client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: iter([
                SimpleNamespace(type="response.completed", response=response)])))
            token = current_observation.set(sink)
            try:
                list(call_model(client, model="fixture", observation_request_type="CONTEXT_RECORD_EXTRACTION"))
                client.responses.create = lambda **kwargs: iter([SimpleNamespace(
                    type="response.failed", response=SimpleNamespace(error=SimpleNamespace(message="fixture")))])
                with self.assertRaises(ModelCallError):
                    list(call_model(client, model="fixture"))
            finally:
                current_observation.reset(token)
            rows = [json.loads(line) for line in (Path(directory)/"observation.jsonl").read_text().splitlines()]
            self.assertEqual(rows[0]["request_type"], "CONTEXT_RECORD_EXTRACTION")
            self.assertEqual(rows[1]["input_tokens"], 10)
            self.assertEqual(rows[-1]["status"], "FAILED")
            self.assertIsNone(rows[-1]["input_tokens"])
