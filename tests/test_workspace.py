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

    def test_read_file_returns_only_requested_line_range(self) -> None:
        """分段读取应返回范围元数据，且不把范围外内容加入结果。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "large.txt"
            target.write_text(
                "第一行\n第二行\n第三行\n第四行\n第五行\n",
                encoding="utf-8",
            )
            context = ToolExecutionContext(InMemoryPlanStore(), root.resolve())

            result = execute_tool_call(
                "read_file",
                '{"path":"large.txt","start_line":2,"line_count":2}',
                context=context,
            )

        self.assertEqual(result.status, "success")
        self.assertIn('lines="2-3"', result.output)
        self.assertIn('total_lines="5"', result.output)
        self.assertIn("第二行\n第三行", result.output)
        self.assertNotIn("第一行", result.output)
        self.assertNotIn("第四行", result.output)

    def test_range_covering_small_file_is_normalized_to_full_read(self) -> None:
        """模型默认传入 1-500 时，小文件应进入完整读取和缓存路径。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "small.py"
            content = "第一行\n第二行\n第三行\n"
            target.write_text(content, encoding="utf-8")
            context = ToolExecutionContext(InMemoryPlanStore(), root.resolve())

            first = execute_tool_call(
                "read_file",
                '{"path":"small.py","start_line":1,"line_count":500}',
                context=context,
            )
            second = execute_tool_call(
                "read_file",
                '{"path":"small.py"}',
                context=context,
            )

        self.assertEqual(first.output, content)
        self.assertIn("<file-unchanged", second.output)
        self.assertNotIn("start_line", second.output)

    def test_read_file_cache_distinguishes_line_ranges(self) -> None:
        """同一文件不同范围不得互相去重，相同范围才返回占位符。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_text(
                "第一行\n第二行\n第三行\n第四行",
                encoding="utf-8",
            )
            context = ToolExecutionContext(InMemoryPlanStore(), root.resolve())

            first_range = execute_tool_call(
                "read_file",
                '{"path":"large.txt","start_line":1,"line_count":2}',
                context=context,
            )
            second_range = execute_tool_call(
                "read_file",
                '{"path":"large.txt","start_line":3,"line_count":2}',
                context=context,
            )
            repeated_range = execute_tool_call(
                "read_file",
                '{"path":"large.txt","start_line":1,"line_count":2}',
                context=context,
            )

        self.assertIn("第一行", first_range.output)
        self.assertIn("第三行", second_range.output)
        self.assertIn("<file-unchanged", repeated_range.output)
        self.assertIn('start_line="1"', repeated_range.output)

    def test_read_file_rejects_invalid_line_range(self) -> None:
        """非法起始行和过大行数应在执行前由参数管线拒绝。"""
        with TemporaryDirectory() as directory:
            context = ToolExecutionContext(
                InMemoryPlanStore(), Path(directory).resolve()
            )

            invalid_start = execute_tool_call(
                "read_file",
                '{"path":"large.txt","start_line":0}',
                context=context,
            )
            invalid_count = execute_tool_call(
                "read_file",
                '{"path":"large.txt","line_count":501}',
                context=context,
            )

        self.assertEqual(invalid_start.status, "invalid")
        self.assertIn("start_line", invalid_start.output)
        self.assertEqual(invalid_count.status, "invalid")
        self.assertIn("line_count", invalid_count.output)

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
