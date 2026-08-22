"""工作区根目录发现测试。"""

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


if __name__ == "__main__":
    unittest.main()
