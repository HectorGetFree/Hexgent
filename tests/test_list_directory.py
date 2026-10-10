"""目录工具的枚举、截断与权限边界。"""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call


class ListDirectoryTest(unittest.TestCase):
    def test_lists_direct_children_and_filters_hidden(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "folder").mkdir()
            (root / "folder" / "nested.txt").touch()
            (root / "a.txt").touch()
            (root / ".hidden").touch()
            (root / "link").symlink_to(root / "folder", target_is_directory=True)
            context = ToolExecutionContext(InMemoryPlanStore(), root)
            result = execute_tool_call("list_directory", '{"path":".","max_entries":200,"include_hidden":false}', context=context)
            self.assertEqual(result.status, "success")
            data = json.loads(result.output)
            self.assertEqual([item["name"] for item in data["entries"]], ["a.txt", "folder", "link"])
            self.assertEqual(data["entries"][-1]["kind"], "symlink")
            self.assertFalse(data["truncated"])
            limited = execute_tool_call("list_directory", '{"path":".","max_entries":1,"include_hidden":false}', context=context)
            self.assertTrue(json.loads(limited.output)["truncated"])
            hidden = execute_tool_call("list_directory", '{"path":".","max_entries":200,"include_hidden":true}', context=context)
            self.assertIn(".hidden", [item["name"] for item in json.loads(hidden.output)["entries"]])

    def test_invalid_limit_and_outside_permission(self):
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(InMemoryPlanStore(), Path(directory))
            invalid = execute_tool_call("list_directory", '{"path":".","max_entries":0,"include_hidden":false}', context=context)
            self.assertEqual(invalid.status, "invalid")
            denied = execute_tool_call("list_directory", '{"path":"..","max_entries":200,"include_hidden":false}', context=context, user_confirmation=lambda *_: "deny")
            self.assertEqual(denied.status, "denied")


if __name__ == "__main__":
    unittest.main()
