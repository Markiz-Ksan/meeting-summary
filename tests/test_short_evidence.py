"""A complete short fact must not fail an arbitrary evidence length floor."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from summary_writer import grounded_summary


class ShortEvidenceTests(unittest.TestCase):
    def render(self, source, summary, evidence):
        return grounded_summary(json.dumps([{'summary': summary, 'evidence': evidence}],
                                           ensure_ascii=False), source)

    def test_complete_short_fact_is_not_rejected_for_quote_length(self):
        try:
            result = self.render('Анна ушла.', 'Анна ушла.', 'Анна ушла.')
        except ValueError as error:
            self.fail('A complete short source was rejected: ' + str(error))
        self.assertEqual(result, '• Анна ушла.')

    def test_tiny_excerpt_of_longer_source_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.render('Анна ушла. Борис остался на встрече.', 'Анна ушла.', 'Анна')

    def test_empty_evidence_is_rejected_even_for_empty_source(self):
        with self.assertRaises(ValueError):
            self.render('', 'Анна ушла.', '')

    def test_punctuation_only_source_does_not_ground_a_fact(self):
        with self.assertRaises(ValueError):
            self.render('............', 'Анна ушла.', '............')

    def test_short_source_still_requires_preserving_negation(self):
        with self.assertRaises(ValueError):
            self.render('Не буду.', 'Буду.', 'Не буду.')


if __name__ == '__main__':
    unittest.main()

class CompleteSentenceEvidenceTests(unittest.TestCase):
    def test_two_short_facts_can_each_use_their_own_complete_evidence(self):
        from summary_writer import write_summary_with
        raw='[{"summary":"Анна ушла.","sources":[1]},{"summary":"Борис остался.","sources":[2]}]'
        self.assertEqual(write_summary_with(lambda _:raw,'Анна ушла. Борис остался.'),'• Анна ушла.\n• Борис остался.')

    def test_unrelated_negative_sentence_does_not_contaminate_short_fact(self):
        self.assertEqual(grounded_summary('[{"summary":"Анна ушла.","evidence":"Анна ушла."}]','Анна ушла. Борис не ушёл.'),'• Анна ушла.')
