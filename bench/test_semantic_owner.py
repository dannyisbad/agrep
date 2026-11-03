import copy
import unittest
from semantic_owner import check


class OwnerSemanticGateTests(unittest.TestCase):
    def setUp(self):
        self.budgets = {f'{mode}_owner_{stat}_ms': 100 for mode in ['semantic', 'hybrid'] for stat in ['p50', 'p95']}
        self.report = {'references': 200000, 'samples': [
            {'mode': mode, 'wall_ms': 50, 'exit': 0, 'meaning_rows': 1}
            for mode in ['semantic', 'hybrid'] for _ in range(20)]}

    def test_complete_owner_workload_passes(self):
        self.assertEqual(check(self.report, self.budgets), [])

    def test_p95_is_recomputed_and_fails_independently_of_median(self):
        for row in self.report['samples'][17:20]:
            row['wall_ms'] = 101
        errors = check(self.report, self.budgets)
        self.assertTrue(any('semantic p95' in e for e in errors))
        self.assertFalse(any('p50' in e for e in errors))

    def test_small_fixture_fallback_missing_samples_and_invalid_times_fail(self):
        for mutate in [lambda r: r.update(references=1000),
                       lambda r: r['samples'].pop(),
                       lambda r: r['samples'][0].update(meaning_rows=0),
                       lambda r: r['samples'][0].update(exit=1),
                       lambda r: r['samples'][0].update(wall_ms=float('nan'))]:
            value = copy.deepcopy(self.report)
            mutate(value)
            self.assertTrue(check(value, self.budgets))

    def test_missing_tail_budget_fails(self):
        del self.budgets['hybrid_owner_p95_ms']
        self.assertTrue(any('hybrid_owner_p95_ms' in e for e in check(self.report, self.budgets)))


if __name__ == '__main__':
    unittest.main()
