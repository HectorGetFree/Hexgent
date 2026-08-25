"""模型工具 Schema 测试。"""

import unittest
from typing import Any

from hexgent.prompt import TOOLS


class ToolSchemaTests(unittest.TestCase):
    """验证严格工具 Schema 满足模型接口约束。"""

    def test_strict_object_schemas_require_every_property(self) -> None:
        """严格模式下，每层对象都必须将全部属性列入 required。"""
        for tool in TOOLS:
            with self.subTest(tool=tool["name"]):
                self._assert_strict_object_schema(tool["parameters"])

    def test_read_file_optional_ranges_are_required_nullable_fields(self) -> None:
        """read_file 使用可空字段表达可选范围参数。"""
        read_file = next(tool for tool in TOOLS if tool["name"] == "read_file")
        parameters = read_file["parameters"]

        self.assertEqual(
            parameters["required"],
            ["path", "start_line", "line_count"],
        )
        self.assertEqual(
            parameters["properties"]["start_line"]["type"],
            ["integer", "null"],
        )
        self.assertEqual(
            parameters["properties"]["line_count"]["type"],
            ["integer", "null"],
        )

    def _assert_strict_object_schema(self, schema: dict[str, Any]) -> None:
        """递归检查对象 Schema，覆盖数组元素中的嵌套对象。"""
        if schema.get("type") == "object":
            properties = schema.get("properties", {})
            self.assertEqual(set(schema.get("required", [])), set(properties))
            for property_schema in properties.values():
                self._assert_strict_object_schema(property_schema)

        items = schema.get("items")
        if isinstance(items, dict):
            self._assert_strict_object_schema(items)


if __name__ == "__main__":
    unittest.main()
