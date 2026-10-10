import unittest

from app.parser import normalize_label


class ParserTest(unittest.TestCase):
    def test_normalize_label_trims_and_lowercases(self) -> None:
        self.assertEqual(normalize_label("  Alpha  "), "alpha")
