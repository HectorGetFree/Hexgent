import json
import pathlib
import unittest
from unittest.mock import patch

from hexgent.integration.capability import CapabilityResult
from hexgent.prompt import TOOLS
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


class _PlanStore:
    pass


class UpdateDocumentWorkspaceCapabilityTest(unittest.TestCase):
    def context(self):
        context = ToolExecutionContext(_PlanStore(), pathlib.Path("/tmp"))
        context.integration_metadata.update(
            {"workspace_run_id": "workspace-run-42", "tool_call_id": "model-call-7"}
        )
        return context

    def arguments(self):
        return json.dumps(
            {
                "documentId": 11,
                "expectedVersionId": 3,
                "content": "updated markdown",
            }
        )

    def test_schema_exposes_exact_versioned_update_contract(self):
        tool = next(tool for tool in TOOLS if tool["name"] == "update_document")
        self.assertEqual(
            {"documentId", "expectedVersionId", "content"},
            set(tool["parameters"]["properties"]),
        )
        self.assertEqual(
            ["documentId", "expectedVersionId", "content"],
            tool["parameters"]["required"],
        )
        self.assertTrue(tool["strict"])
        self.assertFalse(tool["parameters"]["additionalProperties"])
        self.assertIn("VERSION_CONFLICT", tool["description"])
        self.assertIn("expectedVersionId", tool["description"])

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_success_preserves_workspace_run_and_original_call_identity(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1",
            "workspace-run-42",
            "model-call-7",
            "SUCCEEDED",
            {"documentId": 11, "previousVersionId": 3, "versionId": 4},
        )

        result = execute_tool_call(
            "update_document",
            self.arguments(),
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "allow",
        )

        self.assertEqual("success", result.status)
        self.assertEqual(4, result.data["versionId"])
        invoke.assert_called_once_with(
            "workspace-run-42",
            "model-call-7",
            "update_document",
            {"documentId": 11, "expectedVersionId": 3, "content": "updated markdown"},
        )

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_denied_result_is_model_visible_and_does_not_retry(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1",
            "workspace-run-42",
            "model-call-7",
            "DENIED",
            error_code="CAPABILITY_NOT_ALLOWED",
            error_message="capability denied",
        )

        result = execute_tool_call(
            "update_document",
            self.arguments(),
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "allow",
        )

        self.assertEqual("denied", result.status)
        self.assertEqual("CAPABILITY_NOT_ALLOWED", result.failure.code)
        self.assertEqual(1, result.attempt_count)
        invoke.assert_called_once()

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_version_conflict_pending_and_idempotency_failures_are_visible(self, invoke):
        for error_code in (
            "VERSION_CONFLICT",
            "VERSION_NOT_FOUND",
            "DOCUMENT_UPDATE_PENDING",
            "IDEMPOTENCY_CONFLICT",
        ):
            with self.subTest(error_code=error_code):
                invoke.reset_mock()
                invoke.return_value = CapabilityResult(
                    "v1",
                    "workspace-run-42",
                    "model-call-7",
                    "FAILED",
                    error_code=error_code,
                    error_message=error_code,
                )

                result = execute_tool_call(
                    "update_document",
                    self.arguments(),
                    context=self.context(),
                    call_id="model-call-7",
                    user_confirmation=lambda _tool, _message: "allow",
                )

                self.assertEqual("error", result.status)
                self.assertEqual(error_code, result.failure.code)
                self.assertEqual(1, result.attempt_count)
                invoke.assert_called_once()

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_workspace_write_permission_pipeline_can_deny_before_client(self, invoke):
        result = execute_tool_call(
            "update_document",
            self.arguments(),
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "deny",
        )

        self.assertEqual("denied", result.status)
        self.assertEqual("user_denied", result.failure.code)
        invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
