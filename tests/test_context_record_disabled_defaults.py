"""Production默认关闭Session记录自动提取/召回，保留长期Memory。"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from hexgent.config import HexgentConfig,load_config
from hexgent.memory.context_record_extraction import ContextExtractionLimits,prepare_extraction
from hexgent.memory.context_retrieval import ContextRetrievalLimits

class ContextRecordDisabledDefaultsTests(unittest.TestCase):
    def assert_disabled(self,cfg):
        self.assertFalse(cfg.context_extraction.enabled)
        self.assertFalse(cfg.context_retrieval.enabled)
        self.assertTrue(cfg.memory_retrieval.enabled)

    def test_production_constructor_and_repository_defaults(self):
        self.assert_disabled(HexgentConfig())
        self.assert_disabled(load_config(Path(__file__).resolve().parents[1]))

    def test_missing_or_partial_config_remains_disabled(self):
        with TemporaryDirectory() as root:
            self.assert_disabled(load_config(Path(root)))
            (Path(root)/'hexgent.toml').write_text('[context_extraction]\nturn_interval = 5\n[context_retrieval]\nmax_records = 2\n')
            self.assert_disabled(load_config(Path(root)))

    def test_component_defaults_are_disabled(self):
        self.assertFalse(ContextExtractionLimits().enabled)
        self.assertFalse(ContextRetrievalLimits().enabled)

if __name__=='__main__':unittest.main()
