"""冻结数据与独立判定聚合的离线验证。"""
import unittest
from collections import Counter
from .catalog import CASES
from .prepare import assets, quality


class PrepTests(unittest.TestCase):
    def test_sources_and_population(self):
        self.assertEqual(Counter(c['family'] for c in CASES), dict(F1=3, F2=3, F3=3, F4=3))
        count = Counter()
        for case in CASES:
            fixture, facts = assets(case)
            self.assertEqual(len(facts), 7)
            sources = {m['id']: m['text'] for m in fixture['messages']}
            for fact in facts:
                self.assertIn(fact['quote'], sources[fact['source_message_id']])
                count[fact['channel']] += 1
        self.assertEqual(count, dict(SUMMARY_REQUIRED=60, EXTERNALLY_PROTECTED=9, OBSOLETE=15))

    def test_uncertainty_and_missing_claims(self):
        _, facts = assets(CASES[0])
        required = [f for f in facts if f['channel'] == 'SUMMARY_REQUIRED']
        rows = {f['fact_id']: 'PRESENT_CORRECT' for f in required}
        rows[required[0]['fact_id']] = 'UNCERTAIN'
        result = quality(facts, rows)
        self.assertEqual(result['critical_fact_recall'], .8)
        self.assertEqual(result['uncertainty_upper_bound'], 1)
        self.assertIsNone(result['unsupported_claim_rate'])
        self.assertEqual(quality(facts, rows, ['SUPPORTED', 'UNSUPPORTED'])['unsupported_claim_rate'], .5)

    def test_missing_or_invalid_judgment_rejected(self):
        _, facts = assets(CASES[0])
        with self.assertRaises(ValueError):
            quality(facts, {})


if __name__ == '__main__':
    unittest.main()
