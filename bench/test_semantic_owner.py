import copy
import unittest
from semantic_owner import check


class OwnerSemanticCheckTests(unittest.TestCase):
    def setUp(self):
        self.targets = {f'{mode}_owner_{stat}_ms': 100 for mode in ['semantic', 'hybrid'] for stat in ['p50', 'p95']}
        self.report = {'references': 200000, 'samples': [
            {'mode': mode, 'wall_ms': 50, 'exit': 0, 'meaning_rows': 1}
            for mode in ['semantic', 'hybrid'] for _ in range(20)]}

    def test_complete_owner_workload_passes(self):
        errors, lines = check(self.report, self.targets)
        self.assertEqual(errors, [])
        self.assertIn('hybrid p95: 50.000 ms (target 100.000 ms)', lines)

    def test_latency_above_a_target_is_reported_not_failed(self):
        for row in self.report['samples'][17:20]:
            row['wall_ms'] = 101
        errors, lines = check(self.report, self.targets)
        self.assertEqual(errors, [])
        self.assertIn('semantic p95: 101.000 ms (target 100.000 ms, above target)', lines)
        self.assertIn('semantic p50: 50.000 ms (target 100.000 ms)', lines)

    def test_small_fixture_fallback_missing_samples_and_invalid_times_fail(self):
        for mutate in [lambda r: r.update(references=1000),
                       lambda r: r['samples'].pop(),
                       lambda r: r['samples'][0].update(meaning_rows=0),
                       lambda r: r['samples'][0].update(exit=1),
                       lambda r: r['samples'][0].update(wall_ms=float('nan'))]:
            value = copy.deepcopy(self.report)
            mutate(value)
            self.assertTrue(check(value, self.targets)[0])

    def test_missing_tail_target_fails(self):
        del self.targets['hybrid_owner_p95_ms']
        self.assertTrue(any('hybrid_owner_p95_ms' in e for e in check(self.report, self.targets)[0]))


if __name__ == '__main__':
    unittest.main()
