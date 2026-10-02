"""验证 WS 冻结 capability scope 先于 Permission 约束模型与执行路径。"""

from pathlib import Path
from unittest.mock import Mock, patch
import unittest

from hexgent.context import ContextManager
from hexgent.integration.capability import CapabilityResult
from hexgent.loop.agent_turn import _available_tools_for_state
from hexgent.loop.loop_state import LoopState
from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


class WorkspaceCapabilityScopeTest(unittest.TestCase):
    """Capability eligibility 与 Runtime Permission 必须保持正交。"""

    def context(self, *allowed_tools: str) -> ToolExecutionContext:
        context = ToolExecutionContext(InMemoryPlanStore(), Path("/tmp"))
        context.integration_metadata.update(
            {"workspace_run_id": "workspace-run-1", "tool_call_id": "call-1"}
        )
        context.set_workspace_capability_scope(frozenset(allowed_tools))
        return context

    def test_model_visibility_intersects_workspace_scope_but_keeps_internal_tools(self) -> None:
        state = LoopState(
            ContextManager(),
            self.context("search_workspace"),
        )

        names = {tool["name"] for tool in _available_tools_for_state(state)}

        self.assertIn("search_workspace", names)
        self.assertNotIn("list_folder", names)
        self.assertIn("create_plan", names)

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_disallowed_workspace_tool_stops_before_permission_and_gateway(self, invoke: Mock) -> None:
        confirm = Mock(return_value="allow")
        permission_events: list[str] = []

        result = execute_tool_call(
            "list_folder",
            '{"folderId":null,"limit":100}',
            context=self.context("search_workspace"),
            call_id="call-1",
            user_confirmation=confirm,
            permission_event_recorder=lambda event, _payload: permission_events.append(event),
        )

        self.assertEqual("denied", result.status)
        self.assertEqual("CAPABILITY_NOT_ALLOWED", result.failure.code)
        self.assertEqual("eligibility", result.failure.phase)
        confirm.assert_not_called()
        invoke.assert_not_called()
        self.assertEqual([], permission_events)

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_allowed_workspace_tool_still_uses_permission_pipeline(self, invoke: Mock) -> None:
        invoke.return_value = CapabilityResult(
            "v1", "workspace-run-1", "call-1", "SUCCEEDED", {"items": []}
        )
        confirm = Mock(return_value="allow")
        permission_events: list[str] = []

        result = execute_tool_call(
            "list_folder",
            '{"folderId":null,"limit":100}',
            context=self.context("list_folder"),
            call_id="call-1",
            user_confirmation=confirm,
            permission_event_recorder=lambda event, _payload: permission_events.append(event),
        )

        self.assertEqual("success", result.status)
        confirm.assert_called_once()
        self.assertEqual(["permission_evaluated", "permission_resolved"], permission_events)
        invoke.assert_called_once()

    def test_user_deny_remains_permission_failure(self) -> None:
        result = execute_tool_call(
            "list_folder",
            '{"folderId":null,"limit":100}',
            context=self.context("list_folder"),
            call_id="call-1",
            user_confirmation=lambda *_args: "deny",
        )

        self.assertEqual("denied", result.status)
        self.assertEqual("user_denied", result.failure.code)
        self.assertEqual("permission", result.failure.phase)

    def test_legacy_missing_scope_denies_workspace_but_not_internal_tools(self) -> None:
        state = LoopState(ContextManager(), self.context())

        names = {tool["name"] for tool in _available_tools_for_state(state)}

        self.assertNotIn("search_workspace", names)
        self.assertNotIn("list_folder", names)
        self.assertIn("create_plan", names)


if __name__ == "__main__":
    unittest.main()
