"""补测入口的只读保护与配置比较；不调用模型或外部服务。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .baseline_closeout import config_ast, file_hashes


class CloseoutTests(unittest.TestCase):
    def test_config_comparison_ignores_format_but_not_execution_change(self):
        original = 'def full_config(budget):\n    return budget["tokens"]\n'
        formatting = "def full_config( budget ):\n return budget['tokens']\n"
        changed = 'def full_config(budget):\n    return budget["tokens"] + 1\n'
        self.assertEqual(config_ast(original), config_ast(formatting))
        self.assertNotEqual(config_ast(original), config_ast(changed))

    def test_original_hashes_detect_addition_mutation_and_deletion(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            nested = root / 'sessions' / 'transcript.jsonl'
            nested.parent.mkdir()
            nested.write_text('original\n')
            before = file_hashes(root)
            self.assertEqual(before, file_hashes(root))
            self.assertEqual(nested.read_text(), 'original\n')
            nested.write_text('changed\n')
            self.assertNotEqual(before, file_hashes(root))
            nested.write_text('original\n')
            extra = root / 'extra.json'
            extra.write_text('{}')
            self.assertNotEqual(before, file_hashes(root))
            extra.unlink()
            nested.unlink()
            self.assertNotEqual(before, file_hashes(root))


if __name__ == '__main__':
    unittest.main()
