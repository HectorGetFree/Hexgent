"""Hexgent TOML 运行配置加载测试。"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.config import ConfigError, HexgentConfig, load_config


class ConfigTest(unittest.TestCase):
    """验证配置默认值、覆盖规则和错误边界。"""

    def test_missing_default_file_uses_code_defaults(self) -> None:
        """工作区没有默认配置文件时应返回完整代码默认值。"""
        with TemporaryDirectory() as directory:
            config = load_config(Path(directory))

        self.assertEqual(config, HexgentConfig())

    def test_partial_toml_overrides_only_declared_fields(self) -> None:
        """配置文件只覆盖显式字段，其余字段继续使用代码默认值。"""
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "custom.toml"
            config_path.write_text(
                """
[loop]
enforce_run_limits = false

[context]
context_window_tokens = 32000
recent_context_tokens = 8000

[tool_result]
preview_characters = 1000

[tool_parallel]
enabled = false
max_workers = 8
batch_timeout_seconds = 15.0

[tool_retry]
enabled = true
max_attempts = 4
initial_backoff_seconds = 0.1
""".strip(),
                encoding="utf-8",
            )

            config = load_config(root, Path("custom.toml"))

        self.assertFalse(config.loop.enforce_run_limits)
        self.assertEqual(config.loop.max_model_rounds, 100)
        self.assertEqual(config.context.context_window_tokens, 32_000)
        self.assertEqual(config.context.reserved_output_tokens, 8_000)
        self.assertEqual(config.context.reactive_recent_context_tokens, 8_000)
        self.assertEqual(config.tool_result.preview_characters, 1_000)
        self.assertFalse(config.tool_parallel.enabled)
        self.assertEqual(config.tool_parallel.max_workers, 8)
        self.assertEqual(config.tool_parallel.batch_timeout_seconds, 15.0)
        self.assertTrue(config.tool_retry.enabled)
        self.assertEqual(config.tool_retry.max_attempts, 4)
        self.assertEqual(config.tool_retry.initial_backoff_seconds, 0.1)

    def test_unknown_or_invalid_fields_are_rejected(self) -> None:
        """未知字段和错误类型不能被静默忽略。"""
        cases = (
            ("[context]\nunknown = 1", "未知字段"),
            ("[loop]\nmax_model_rounds = '100'", "必须是整数"),
            ("[tool_result]\npreview_characters = 0", "必须大于 0"),
            ("[tool_parallel]\nmax_workers = 0", "必须大于 0"),
            ("[tool_retry]\nmax_attempts = 0", "必须大于 0"),
            (
                "[context]\ncontext_window_tokens = 10000\n"
                "reserved_output_tokens = 8000\nsafety_margin_tokens = 4000",
                "必须小于上下文窗口",
            ),
            (
                "[context]\nrecent_context_tokens = 4000\n"
                "reactive_recent_context_tokens = 5000",
                "不能大于 recent_context_tokens",
            ),
        )
        for content, expected_message in cases:
            with self.subTest(content=content), TemporaryDirectory() as directory:
                root = Path(directory)
                config_path = root / "invalid.toml"
                config_path.write_text(content, encoding="utf-8")

                with self.assertRaisesRegex(ConfigError, expected_message):
                    load_config(root, config_path)


if __name__ == "__main__":
    unittest.main()
