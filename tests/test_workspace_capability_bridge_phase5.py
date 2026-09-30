import unittest
from unittest.mock import patch

from hexgent.integration.capability import CapabilityResult
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.tool.tool_registry import get_tool


class _PlanStore:
    pass


class WorkspaceCapabilityBridgePhase5Test(unittest.TestCase):
    def context(self):
        context = ToolExecutionContext(_PlanStore(), __import__("pathlib").Path("/tmp"))
        context.integration_metadata.update({"workspace_run_id": "ws-run", "tool_call_id": "call-1"})
        return context

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_existing_tool_pipeline_propagates_run_and_call_and_maps_success(self, invoke):
        invoke.return_value = CapabilityResult("v1", "ws-run", "call-1", "SUCCEEDED", {"ok": True})
        result = execute_tool_call(
            "workspace_capability", '{"capability":"phase5_echo","arguments":{"message":"hi"}}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("success", result.status)
        invoke.assert_called_once_with("ws-run", "call-1", "phase5_echo", {"message": "hi"})

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_denied_and_failed_are_model_visible_tool_results(self, invoke):
        invoke.return_value = CapabilityResult("v1", "ws-run", "call-1", "DENIED",
                                               error_code="CAPABILITY_NOT_ALLOWED", error_message="denied")
        denied = execute_tool_call(
            "workspace_capability", '{"capability":"phase5_echo","arguments":{}}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("denied", denied.status)
        self.assertEqual("CAPABILITY_NOT_ALLOWED", denied.failure.code)
        invoke.return_value = CapabilityResult("v1", "ws-run", "call-1", "FAILED",
                                               error_code="HANDLER_ERROR", error_message="failed")
        failed = execute_tool_call(
            "workspace_capability", '{"capability":"phase5_echo","arguments":{}}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("error", failed.status)
        self.assertEqual("HANDLER_ERROR", failed.failure.code)
        self.assertTrue(failed.failure.retryable)

        invoke.return_value = CapabilityResult(
            "v1", "ws-run", "call-1", "FAILED",
            error_code="D404", error_message="文档不存在"
        )
        not_found = execute_tool_call(
            "workspace_capability", '{"capability":"get_document","arguments":{}}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("error", not_found.status)
        self.assertEqual("D404", not_found.failure.code)
        self.assertFalse(not_found.failure.retryable)

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_search_workspace_uses_existing_capability_pipeline(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1", "ws-run", "call-1", "SUCCEEDED", {"items": [{"content": "命中"}]}
        )
        result = execute_tool_call(
            "search_workspace", '{"query":"检索内容","topK":5}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("success", result.status)
        invoke.assert_called_once_with(
            "ws-run", "call-1", "search_workspace", {"query": "检索内容", "topK": 5}
        )

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_get_document_uses_exact_identity_and_range(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1", "ws-run", "call-1", "SUCCEEDED", {"content": "指定版本内容"}
        )
        result = execute_tool_call(
            "get_document",
            '{"documentId":11,"versionId":3,"startLine":2,"lineCount":20}',
            context=self.context(),
            call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("success", result.status)
        invoke.assert_called_once_with(
            "ws-run", "call-1", "get_document",
            {"documentId": 11, "versionId": 3, "startLine": 2, "lineCount": 20},
        )


if __name__ == "__main__":
    unittest.main()
