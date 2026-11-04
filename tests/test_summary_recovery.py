"""No model imports, audio devices, playback or real meeting data."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from worker import Engine


class SummaryRecoveryTests(unittest.TestCase):
    def engine(self):
        engine = Engine.__new__(Engine)
        engine.model = object()
        engine.tokenizer = engine.sampler = None
        engine.mx = SimpleNamespace(clear_cache=Mock())
        return engine

    def test_link_checks_alone_do_not_generate_or_report_rejected_summary(self):
        engine = self.engine()
        source = 'Всем привет. Меня слышно? Да, слышно. Тогда ждём остальных.'
        with patch('summary_writer.write_summary', side_effect=AssertionError('No substantive source')) as writer:
            self.assertEqual(engine.summarize(source), '')
        writer.assert_not_called()

    def test_link_check_does_not_hide_following_meeting_fact(self):
        engine = self.engine()
        with patch('summary_writer.write_summary', return_value='• Встреча в пятницу.') as writer:
            self.assertEqual(engine.summarize('Да, слышно. Встреча в пятницу.'), '• Встреча в пятницу.')
        self.assertEqual(writer.call_args.args[2], 'Встреча в пятницу.')

    def test_waiting_for_confirmation_is_not_social_noise(self):
        engine = self.engine()
        source = 'Тогда ждём подтверждения склада.'
        with patch('summary_writer.write_summary', return_value='• Ждут подтверждения склада.') as writer:
            engine.summarize(source)
        self.assertEqual(writer.call_args.args[2], source)

    def test_greeting_with_fact_in_same_sentence_is_preserved(self):
        engine = self.engine()
        source = 'Всем привет, бюджет 100 тысяч рублей.'
        with patch('summary_writer.write_summary', return_value='• Бюджет 100 тысяч рублей.') as writer:
            engine.summarize(source)
        self.assertEqual(writer.call_args.args[2], source)

    def test_rejected_and_failed_drafts_clear_cache_without_replacing_model(self):
        for error in [ValueError('Rejected draft'), RuntimeError('Generation failed')]:
            with self.subTest(error=type(error).__name__):
                engine = self.engine()
                loaded_model = engine.model
                with patch('summary_writer.write_summary', side_effect=error):
                    with self.assertRaises(type(error)) as caught:
                        engine.summarize('Встреча в пятницу.')
                self.assertIs(caught.exception, error)
                self.assertIs(engine.model, loaded_model)
                engine.mx.clear_cache.assert_called_once_with()

    def test_successful_draft_still_clears_cache_once(self):
        engine = self.engine()
        with patch('summary_writer.write_summary', return_value='• Встреча в пятницу.'):
            self.assertEqual(engine.summarize('Встреча в пятницу.'), '• Встреча в пятницу.')
        engine.mx.clear_cache.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
