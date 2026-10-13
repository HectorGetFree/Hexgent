import unittest
import json
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from hexgent.integration.capability import CapabilityResult
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.tool.tool_registry import get_tool
from hexgent.tool.tool_execution import execute_tool_batch
from hexgent.tool.tool_scheduler import ToolParallelLimits
from hexgent.tool.workspace_capability_tool import invoke_workspace_capability


class _PlanStore:
    pass


class WorkspaceCapabilityBridgePhase5Test(unittest.TestCase):
    def test_parallel_documents_keep_request_identity(self):
        """同时进入桥接函数后，远程请求和模型结果仍各自匹配调用身份。"""
        context = self.context()
        barrier = Barrier(2)
        received = []

        def bridge(capability, arguments, call_context):
            barrier.wait(timeout=3)
            return invoke_workspace_capability(capability, arguments, call_context)

        def invoke(run_id, call_id, capability, arguments):
            received.append((call_id, arguments['documentId']))
            return CapabilityResult('v1', run_id, call_id, 'SUCCEEDED', arguments)

        calls = [SimpleNamespace(call_id=f'call-{i}', name='get_document',
                 arguments=json.dumps(dict(documentId=i, versionId=i + 10,
                                           startLine=1, lineCount=200))) for i in (1, 2)]
        with patch('hexgent.tool.workspace_capability_tool.invoke_workspace_capability', side_effect=bridge), \
             patch('hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke', side_effect=invoke):
            generator = execute_tool_batch(calls, context=context,
                user_confirmation=lambda _tool, _message: 'allow',
                parallel_limits=ToolParallelLimits(enabled=True, max_workers=2),
                remaining_tool_calls=None, deadline=None)
            while True:
                try:
                    next(generator)
                except StopIteration as stopped:
                    batch = stopped.value
                    break
        self.assertEqual(sorted(received), [('call-1', 1), ('call-2', 2)])
        self.assertEqual([item['call_id'] for item in batch.outputs], ['call-1', 'call-2'])
        self.assertEqual([json.loads(item['output'])['documentId'] for item in batch.outputs], [1, 2])
        self.assertEqual(context.integration_metadata['tool_call_id'], 'call-1')
        scoped = context.for_tool_call('isolated')
        self.assertIs(scoped.cache_lock, context.cache_lock)
        self.assertIs(scoped.tool_call_cache, context.tool_call_cache)
        self.assertIs(scoped.plan_store, context.plan_store)
        self.assertTrue(get_tool('get_document').concurrency_safe)

    def context(self):
        context = ToolExecutionContext(_PlanStore(), __import__("pathlib").Path("/tmp"))
        context.integration_metadata.update({"workspace_run_id": "ws-run", "tool_call_id": "call-1"})
        context.set_workspace_capability_scope(frozenset({
            "workspace_capability", "search_workspace", "get_document", "list_folder",
        }))
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

    @patch("hexgent.tool.workspace_capability_tool.WorkspaceCapabilityClient.invoke")
    def test_list_folder_uses_root_and_preserves_identity(self, invoke):
        invoke.return_value = CapabilityResult(
            "v1", "ws-run", "call-1", "SUCCEEDED", {"items": []}
        )
        result = execute_tool_call(
            "list_folder", '{"folderId":null,"limit":100}',
            context=self.context(), call_id="call-1",
            user_confirmation=lambda _tool, _message: "allow",
        )
        self.assertEqual("success", result.status)
        invoke.assert_called_once_with(
            "ws-run", "call-1", "list_folder", {"limit": 100}
        )


if __name__ == "__main__":
    unittest.main()
