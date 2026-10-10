"""生产诊断不干预执行、不伪造用量的回归。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from hexgent.observation import RunObservation, begin_span, end_span, observed_handler, summarize


class RuntimeObservationTests(unittest.TestCase):
    def test_handler_threads_have_explicit_sink_and_distinct_call_ids(self):
        with TemporaryDirectory() as directory:
            sink = RunObservation(SimpleNamespace(session_dir=Path(directory)))
            context = SimpleNamespace(observation=sink)
            barrier = Barrier(2)
            def handler(args, context):
                barrier.wait(timeout=2)
                return args["value"]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(observed_handler, handler, {"value": value}, context,
                                       str(value), 1, "fixture") for value in (1, 2)]
                self.assertEqual([future.result() for future in futures], [1, 2])
            rows = [json.loads(line) for line in (Path(directory)/"observation.jsonl").read_text().splitlines()]
            self.assertEqual({row["call_id"] for row in rows}, {"1", "2"})
            self.assertEqual(summarize(rows)[sink.execution_id]["handler_peak_concurrency"], 2)

    def test_missing_usage_is_null_and_separate_from_observed_subtotal(self):
        with TemporaryDirectory() as directory:
            sink = RunObservation(SimpleNamespace(session_dir=Path(directory)))
            for usage in (None, {"input_tokens": 12, "output_tokens": 3}):
                span = begin_span("provider", observer=sink, request_type="MAIN_AGENT")
                end_span(span, status="SUCCEEDED", response=SimpleNamespace(usage=usage))
            rows = [json.loads(line) for line in (Path(directory)/"observation.jsonl").read_text().splitlines()]
            value = summarize(rows)[sink.execution_id]["provider"]["MAIN_AGENT"]
            self.assertIsNone(value["input_tokens_total"])
            self.assertEqual(value["input_tokens_observed_subtotal"], 12)
            self.assertEqual(value["missing_usage_requests"], 1)

    def test_observation_failure_does_not_change_handler_return(self):
        sink = RunObservation(SimpleNamespace(session_dir=Path("/unused")))
        with patch.object(Path, "mkdir", side_effect=OSError("fixture")), self.assertLogs("hexgent.observation"):
            result = observed_handler(lambda args, context: "original", {},
                SimpleNamespace(observation=sink), "call-1", 1, "test")
        self.assertEqual(result, "original")
        self.assertFalse(sink.available)

    def test_handler_exception_and_attempt_are_preserved(self):
        with TemporaryDirectory() as directory:
            sink = RunObservation(SimpleNamespace(session_dir=Path(directory)))
            def broken(args, context):
                raise ValueError("original")
            with self.assertRaisesRegex(ValueError, "original"):
                observed_handler(broken, {}, SimpleNamespace(observation=sink), "call-2", 2, "test")
            rows = [json.loads(line) for line in (Path(directory)/"observation.jsonl").read_text().splitlines()]
            self.assertEqual(rows[-1]["status"], "RAISED")
            self.assertEqual(rows[-1]["attempt"], 2)
            self.assertEqual(summarize(rows)[sink.execution_id]["open_span_count"], 0)
