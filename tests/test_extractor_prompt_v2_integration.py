"""验证默认 Production 请求逐字使用冻结 v2，而非 Eval 覆盖。"""
from hashlib import sha256
import json
from pathlib import Path
import unittest

from hexgent.context import ContextManager, ContextLimits
from hexgent.context.key_fact_extractor import EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION, extract_snapshot
from tests.test_key_fact_remediation import Model


class ExtractorPromptV2IntegrationTests(unittest.TestCase):
    def test_default_prompt_equals_frozen_request_and_hash(self):
        root=Path(__file__).resolve().parents[1]
        frozen=json.loads((root/'eval/context_remediation_v1/runs/extractor-quality-pilot-v1-20261012/requests/10.json').read_text())
        self.assertEqual(EXTRACTION_INSTRUCTIONS,frozen['instructions'])
        self.assertEqual(sha256(EXTRACTION_INSTRUCTIONS.encode()).hexdigest(),'4eabf33ac2ee71a095bea0cea62b5dc130da14a0924db681301a796e9adeeef6')
        self.assertEqual(EXTRACTION_PROMPT_VERSION,'key-fact-extraction-v2')

    def test_production_extraction_request_without_adapter(self):
        manager=ContextManager()
        manager.append_user_query('处理交接')
        manager.append_messages([dict(role='assistant',content='当前交接任务 AUDIT-MAPLE-57')],completes_round=True)
        cut=len(manager.active_messages)
        manager.append_user_query('继续')
        model=Model()
        snapshot,ledger=extract_snapshot(model,'offline',manager,cut,ContextLimits())
        self.assertEqual(len(model.calls),1)
        self.assertEqual(model.calls[0]['instructions'],EXTRACTION_INSTRUCTIONS)
        self.assertEqual(ledger['prompt_version'],'key-fact-extraction-v2')
        self.assertEqual(ledger['status'],'VALIDATED')
        self.assertTrue(snapshot['facts'])
        self.assertIsNone(manager.key_fact_snapshot)
        self.assertEqual(model.options,[{'timeout':120.0,'max_retries':0}])

    def test_historical_pilot_does_not_double_append_v2(self):
        from eval.context_remediation_v1.factorial_pilot import EXTRACTION_INSTRUCTIONS as old, PROMPT_ADDITION
        self.assertNotEqual(old,EXTRACTION_INSTRUCTIONS)
        self.assertEqual(old+PROMPT_ADDITION,EXTRACTION_INSTRUCTIONS)
        from eval.context_remediation_v1.rl3_e2e import V2
        self.assertEqual(V2,EXTRACTION_INSTRUCTIONS)


if __name__=='__main__':unittest.main()
