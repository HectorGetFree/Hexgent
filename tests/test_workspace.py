"""工作区根目录发现测试。"""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.plan import InMemoryPlanStore
from hexgent.tool.tool_context import ToolExecutionContext
from hexgent.tool.tool_execution import execute_tool_call
from hexgent.workspace import find_workspace_root


class WorkspaceTest(unittest.TestCase):
    """验证从包子目录启动时仍能定位项目根目录。"""

    def test_find_project_root_from_nested_package_directory(self) -> None:
        """应返回最近包含 pyproject.toml 的父目录。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "hexgent" / "nested"
            package.mkdir(parents=True)
            (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

            resolved = find_workspace_root(package)

        self.assertEqual(resolved, root.resolve())

    def test_file_tool_uses_context_workdir_for_relative_path(self) -> None:
        """文件工具执行路径应与权限判断使用同一个工作区。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.txt").write_text("工作区内容", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root.resolve())

            result = execute_tool_call(
                "read_file",
                '{"path":"example.txt"}',
                context=context,
            )

        self.assertEqual(result.status, "success")
        self.assertEqual(result.output, "工作区内容")

    def test_repeated_read_returns_unchanged_stub(self) -> None:
        """相同文件内容不应重复进入模型上下文。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.txt"
            target.write_text("不会重复返回的内容", encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root.resolve())

            first = execute_tool_call(
                "read_file",
                '{"path":"example.txt"}',
                context=context,
            )
            second = execute_tool_call(
                "read_file",
                '{"path":"example.txt"}',
                context=context,
            )

        self.assertEqual(first.output, "不会重复返回的内容")
        self.assertIn("<file-unchanged", second.output)
        self.assertNotIn("不会重复返回的内容", second.output)

    def test_hash_detects_changed_content_with_same_size_and_modified_time(self) -> None:
        """即使大小和修改时间不变，内容 Hash 变化仍应重新返回全文。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "example.txt"
            target.write_text("AAAA", encoding="utf-8")
            original_stat = target.stat()
            context = ToolExecutionContext(InMemoryPlanStore(), root.resolve())
            execute_tool_call(
                "read_file",
                '{"path":"example.txt"}',
                context=context,
            )

            target.write_text("BBBB", encoding="utf-8")
            os.utime(
                target,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            changed = execute_tool_call(
                "read_file",
                '{"path":"example.txt"}',
                context=context,
            )

        self.assertEqual(changed.output, "BBBB")


if __name__ == "__main__":
    unittest.main()
