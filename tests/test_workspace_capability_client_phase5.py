import unittest
from unittest.mock import patch

from hexgent.integration.capability import WorkspaceCapabilityClient


class WorkspaceCapabilityClientPhase5Test(unittest.TestCase):
    @patch("hexgent.integration.capability.httpx.post")
    def test_maps_succeeded_result(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "protocolVersion": "v1", "runId": "r1", "callId": "c1",
            "status": "SUCCEEDED", "data": {"ok": True},
        }
        result = WorkspaceCapabilityClient("http://ws", "token").invoke("r1", "c1", "echo", {})
        self.assertEqual("SUCCEEDED", result.status)
        post.assert_called_once()
        self.assertEqual("r1", post.call_args.kwargs["json"]["runId"])

    @patch("hexgent.integration.capability.httpx.post")
    def test_rejects_mismatched_response_identity(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {
            "protocolVersion": "v1", "runId": "other", "callId": "c1", "status": "SUCCEEDED",
        }
        result = WorkspaceCapabilityClient("http://ws", "token").invoke("r1", "c1", "echo", {})
        self.assertEqual("PROTOCOL_ERROR", result.error_code)
