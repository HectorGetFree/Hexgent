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


class CreateDocumentWorkspaceCapabilityTest(unittest.TestCase):
    """验证 create_document 复用受控 Workspace Capability 管线。"""

    def context(self):
        context = ToolExecutionContext(_PlanStore(), pathlib.Path("/tmp"))
        context.integration_metadata.update(
            {"workspace_run_id": "workspace-run-42", "tool_call_id": "model-call-7"}
        )
        return context

    def arguments(self):
        return json.dumps(
            {
                "parentFolderId": 8,
                "title": "Agent 创建的文档",
                "content": "# 正式内容\n\n由 Agent 创建。",
            }
        )

    def test_schema_exposes_only_create_contract(self):
        tool = next(tool for tool in TOOLS if tool["name"] == "create_document")

        self.assertEqual(
            {"parentFolderId", "title", "content"},
            set(tool["parameters"]["properties"]),
        )
        self.assertEqual(
            ["parentFolderId", "title", "content"],
            tool["parameters"]["required"],
        )
        self.assertTrue(tool["strict"])
        self.assertFalse(tool["parameters"]["additionalProperties"])

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_success_preserves_workspace_run_and_original_call_identity(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1",
            "workspace-run-42",
            "model-call-7",
            "SUCCEEDED",
            {"documentId": 21, "versionId": 34},
        )

        result = execute_tool_call(
            "create_document",
            self.arguments(),
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "allow",
        )

        self.assertEqual("success", result.status)
        self.assertEqual({"documentId": 21, "versionId": 34}, result.data)
        invoke.assert_called_once_with(
            "workspace-run-42",
            "model-call-7",
            "create_document",
            {
                "parentFolderId": 8,
                "title": "Agent 创建的文档",
                "content": "# 正式内容\n\n由 Agent 创建。",
            },
        )

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_workspace_write_permission_can_deny_before_client(self, invoke):
        result = execute_tool_call(
            "create_document",
            self.arguments(),
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "deny",
        )

        self.assertEqual("denied", result.status)
        self.assertEqual("user_denied", result.failure.code)
        invoke.assert_not_called()

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_ws_denied_is_model_visible_permission_failure(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1",
            "workspace-run-42",
            "model-call-7",
            "DENIED",
            error_code="CAPABILITY_NOT_ALLOWED",
            error_message="capability denied",
        )

        result = execute_tool_call(
            "create_document",
            self.arguments(),
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "allow",
        )

        self.assertEqual("denied", result.status)
        self.assertEqual("CAPABILITY_NOT_ALLOWED", result.failure.code)
        self.assertEqual("permission", result.failure.phase)
        self.assertEqual(1, result.attempt_count)
        invoke.assert_called_once()

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_pending_and_idempotency_conflict_are_visible_without_auto_retry(self, invoke):
        for error_code in ("DOCUMENT_CREATE_PENDING", "IDEMPOTENCY_CONFLICT"):
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
                    "create_document",
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
    def test_rejects_forged_authority_and_internal_fields_before_client(self, invoke):
        arguments = json.dumps(
            {
                "parentFolderId": 8,
                "title": "Agent 创建的文档",
                "content": "正文",
                "userId": "forged-user",
                "workspaceId": 999,
                "runId": "forged-run",
                "callId": "forged-call",
            }
        )

        result = execute_tool_call(
            "create_document",
            arguments,
            context=self.context(),
            call_id="model-call-7",
            user_confirmation=lambda _tool, _message: "allow",
        )

        self.assertEqual("invalid", result.status)
        self.assertEqual("invalid_arguments", result.failure.code)
        invoke.assert_not_called()


if __name__ == "__main__":
    unittest.main()
