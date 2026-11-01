import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('evaluate_summaries', ROOT / 'scripts/evaluate_summaries.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


class AcceptanceCorpusTests(unittest.TestCase):
    def test_development_is_fixed_and_annotated_before_evaluation(self):
        cases = evaluation.load_cases('development')
        self.assertEqual(len(cases), 22)
        self.assertEqual(len({c['source'] for c in cases}), 22)
        for case in cases:
            self.assertEqual(case['split'], 'development')
            self.assertTrue(case['forbidden_inferences'])
            self.assertTrue(case['expect_empty'] or case['critical_facts'])

    def test_holdout_is_unavailable_without_explicit_freeze(self):
        with self.assertRaisesRegex(ValueError, 'requires --freeze'):
            evaluation.load_cases('holdout')

    def test_stale_freeze_cannot_authorize_holdout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'freeze.json'
            path.write_text(json.dumps(dict(writer_sha256='stale', development_review_status='passed')))
            with self.assertRaisesRegex(ValueError, 'frozen version'):
                evaluation.load_cases('holdout', path)


if __name__ == '__main__':
    unittest.main()
