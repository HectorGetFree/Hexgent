"""向 Workspace 投递安全 PermissionRequest 摘要。"""
import json
from urllib.request import Request, urlopen


class WorkspacePermissionClient:
    def __init__(self, url: str, token: str, timeout_seconds: float = 3.0) -> None:
        self.url, self.token, self.timeout_seconds = url, token, timeout_seconds

    def publish(self, *, run_id: str, request_id: str, tool_name: str, description: str, created_at: str) -> None:
        request = Request(self.url, data=json.dumps({
            "integrationRunId": run_id, "requestId": request_id,
            "toolName": tool_name, "description": description, "createdAt": created_at,
        }).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}, method="POST")
        with urlopen(request, timeout=self.timeout_seconds):
            return
