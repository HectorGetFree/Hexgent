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
        self.assertEqual("error", denied.status)
        self.assertEqual("permission_denied", denied.failure.code)
        invoke.return_value = CapabilityResult("v1", "ws-run", "call-1", "FAILED",
                                               error_code="HANDLER_ERROR", error_message="failed")
        failed = execute_tool_call(
            "workspace_capability", '{"capability":"phase5_echo","arguments":{}}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("error", failed.status)


if __name__ == "__main__":
    unittest.main()
