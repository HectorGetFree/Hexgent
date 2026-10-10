"""Provider preflight 的本地、无网络合同测试。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from .provider_preflight import (
    classify_preflight_failure,
    redact_diagnostic,
    run_preflight,
    write_preflight_artifact,
)


class _CompletedEvent:
    type = "response.completed"
    response = SimpleNamespace(usage=object())


class _FailedEvent:
    type = "response.failed"
    response = SimpleNamespace(error=SimpleNamespace(message="Request blocked by upstream policy"))
    error = None


class _Responses:
    def __init__(self, response: object) -> None:
        self.response = response
        self.request: dict | None = None

    def create(self, **kwargs: object) -> object:
        self.request = kwargs
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class _Client:
    def __init__(self, response: object) -> None:
        self.responses = _Responses(response)


class ProviderPreflightTest(unittest.TestCase):
    def test_classifies_explicit_provider_failures(self) -> None:
        self.assertEqual(classify_preflight_failure("Request blocked by upstream policy"), "PROVIDER_UPSTREAM_POLICY_BLOCK")
        self.assertEqual(classify_preflight_failure("Provider is overloaded (503)"), "PROVIDER_OVERLOADED")
        self.assertEqual(classify_preflight_failure("account is no longer enabled for this key"), "PROVIDER_ACCOUNT_DISABLED")
        self.assertEqual(classify_preflight_failure("previous response account binding changed"), "PROVIDER_RESPONSE_BINDING_ERROR")
        self.assertEqual(classify_preflight_failure("Request timed out"), "PROVIDER_TIMEOUT")
        self.assertEqual(classify_preflight_failure("peer closed incomplete chunked response"), "PROVIDER_STREAM_INTERRUPTED")
        self.assertEqual(classify_preflight_failure("unexpected validator result"), "UNKNOWN")

    def test_preflight_uses_explicit_model_and_never_retries_account_disabled(self) -> None:
        client = _Client(RuntimeError("account is no longer enabled for this key"))
        result = run_preflight(client, model="luna-reserve")

        self.assertEqual(result["failure_category"], "PROVIDER_ACCOUNT_DISABLED")
        self.assertFalse(result["retryable"])
        self.assertEqual(client.responses.request["model"], "luna-reserve")

    def test_preflight_success_is_separate_and_artifact_is_sanitized(self) -> None:
        client = _Client([_CompletedEvent()])
        result = run_preflight(client, model="luna-reserve")
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["usage_available"])

        with TemporaryDirectory() as directory:
            path = write_preflight_artifact(
                Path(directory) / "preflight.json",
                {"diagnostic": "Bearer secret-token sk-abcdefghijklmnop"},
            )
            artifact = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(artifact["kind"], "PROVIDER_PREFLIGHT")
        self.assertFalse(artifact["included_in_benchmark_statistics"])
        self.assertNotIn("secret-token", json.dumps(artifact))
        self.assertNotIn("sk-abcdefghijklmnop", json.dumps(artifact))

    def test_preflight_preserves_terminal_event_diagnostic(self) -> None:
        result = run_preflight(_Client([_FailedEvent()]), model="luna-reserve")

        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["failure_category"], "PROVIDER_UPSTREAM_POLICY_BLOCK")
        self.assertEqual(result["diagnostic"]["event_type"], "response.failed")
        self.assertIn("response.failed", result["event_types"])

    def test_redaction_removes_nested_credentials(self) -> None:
        redacted = redact_diagnostic({"Authorization": "Bearer abc", "nested": {"api_key": "secret"}})
        self.assertEqual(redacted["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["api_key"], "[REDACTED]")
