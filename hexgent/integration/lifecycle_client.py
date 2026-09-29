"""向 Workspace 投递 LifecycleEvent 的最小 HTTP 客户端。"""

from __future__ import annotations

import json
from urllib.request import Request, urlopen

from .lifecycle import LifecycleEvent


class WorkspaceLifecycleClient:
    """使用 HTTP callback 投递生命周期事件；失败不改变 Runtime 事实。"""

    def __init__(self, url: str, token: str, timeout_seconds: float = 3.0) -> None:
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds

    def publish(self, event: LifecycleEvent) -> None:
        request = Request(
            self.url,
            data=json.dumps(
                {
                    "eventId": event.event_id,
                    "sequence": event.sequence,
                    "workspaceRunId": event.workspace_run_id,
                    "conversationId": event.conversation_id,
                    "type": event.type,
                    "occurredAt": event.occurred_at,
                    "causedByCommandId": event.caused_by_command_id,
                    "payload": event.payload,
                }
            ).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds):
            return
