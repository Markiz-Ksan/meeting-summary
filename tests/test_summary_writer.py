import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from summary_writer import format_summary


class SummaryFormattingTests(unittest.TestCase):
    def test_plain_generated_summary_is_displayed(self):
        self.assertEqual(format_summary('Дату запуска пока не определили.'), '• Дату запуска пока не определили.')

    def test_numbered_output_is_normalized(self):
        self.assertEqual(format_summary('1. Анна проверит смету.\n2. Бюджет пока не утверждён.'), '• Анна проверит смету.\n• Бюджет пока не утверждён.')

    def test_unfinished_thought_never_leaks_to_display(self):
        self.assertEqual(format_summary('<think>Нужно рассуждать об исходном тексте'), '')
        self.assertEqual(format_summary('<think>Скрытые мысли.</think>\n• Дату ещё не согласовали.'), '• Дату ещё не согласовали.')

    def test_more_than_five_bullets_preserve_last_qualifier(self):
        text = '\n'.join(f'• Пункт {index} пока не подтверждён.' for index in range(7))
        result = format_summary(text)
        self.assertEqual(len(result.splitlines()),5)
        self.assertIn('Пункт 6 пока не подтверждён.',result)


class GroundedSummaryTests(unittest.TestCase):
    def render(self, source, items):
        import json, types
        from unittest.mock import patch
        from summary_writer import write_summary
        tokenizer = types.SimpleNamespace(apply_chat_template=lambda *a, **k: 'prompt')
        with patch.dict(sys.modules, {'mlx_lm': types.SimpleNamespace(generate=lambda *a, **k: json.dumps(items, ensure_ascii=False))}):
            return write_summary(None, tokenizer, source, None)

    def test_grounded_paraphrase_displays_without_json(self):
        self.assertEqual(self.render('Склад не подтвердил наличие. Доставку пока не обещаем.',
            [{'summary':'Доставку пока не обещают: наличие не подтверждено.', 'evidence':'Склад не подтвердил наличие. Доставку пока не обещаем.'}]),
            '• Доставку пока не обещают: наличие не подтверждено.')

    def test_invented_quote_is_not_displayed(self):
        with self.assertRaises(ValueError):
            self.render('Решения ещё нет.', [{'summary':'Релиз согласован на пятницу.', 'evidence':'Релиз согласован на пятницу.'}])

    def test_invented_amount_is_not_displayed(self):
        with self.assertRaises(ValueError):
            self.render('Бюджет 100 тысяч рублей.', [{'summary':'Бюджет 500 тысяч рублей.', 'evidence':'Бюджет 100 тысяч рублей.'}])

    def test_proposal_must_not_become_decision(self):
        with self.assertRaises(ValueError):
            self.render('Предлагаю пятницу. Решение не принято.', [{'summary':'Решили провести встречу в пятницу.', 'evidence':'Предлагаю пятницу. Решение не принято.'}])

    def test_added_causal_link_is_rejected(self):
        with self.assertRaises(ValueError):
            self.render('Бюджет не изменили. Смета будет в среду.', [{'summary':'Бюджет не изменили, так как смета будет в среду.', 'evidence':'Бюджет не изменили. Смета будет в среду.'}])

    def test_unspoken_temporal_dependency_is_rejected(self):
        with self.assertRaises(ValueError):
            self.render('Решение не принято. Смета будет в среду.', [{'summary':'Решение не принято, ожидается после сметы.', 'evidence':'Решение не принято. Смета будет в среду.'}])

class SummaryIntegrityTests(unittest.TestCase):
    def render(self, source, records):
        import json
        from summary_writer import grounded_summary
        return grounded_summary(json.dumps(records,ensure_ascii=False),source)

    def test_invalid_item_cannot_silently_remove_important_qualifier(self):
        with self.assertRaises(ValueError):
            self.render('Смета будет в среду. Решение не принято.', [
                {'summary':'Смета будет в среду.', 'evidence':'Смета будет в среду.'},
                {'summary':'Решение принято.', 'evidence':'Решение не принято.'}])

    def test_missing_negation_is_not_saved_by_unrelated_pending_word(self):
        with self.assertRaises(ValueError):
            self.render('Бюджет не согласован.', [{'summary':'Бюджет пока согласован.', 'evidence':'Бюджет не согласован.'}])

    def test_punctuation_variants_are_not_repeated(self):
        result=self.render('Встреча будет в пятницу.',[
            {'summary':'Встреча будет в пятницу.', 'evidence':'Встреча будет в пятницу.'},
            {'summary':'Встреча будет в пятницу!', 'evidence':'Встреча будет в пятницу.'}])
        self.assertEqual(result,'• Встреча будет в пятницу.')

    def test_unbounded_paragraph_is_rejected_instead_of_truncated(self):
        with self.assertRaises(ValueError):
            self.render('Обсуждали сроки проекта.',[{'summary':'Сроки проекта обсуждали. '*35,'evidence':'Обсуждали сроки проекта.'}])

    def test_condition_cannot_be_dropped(self):
        with self.assertRaises(ValueError):
            self.render('Если склад подтвердит наличие, отправим товар в пятницу.', [{'summary':'Товар отправят в пятницу.', 'evidence':'Если склад подтвердит наличие, отправим товар в пятницу.'}])

    def test_currency_cannot_be_replaced(self):
        with self.assertRaises(ValueError):
            self.render('Стоимость 100 рублей.', [{'summary':'Стоимость 100 долларов.', 'evidence':'Стоимость 100 рублей.'}])

    def test_existing_meeting_is_not_reported_as_rescheduled(self):
        with self.assertRaises(ValueError):
            self.render('Встреча будет в пятницу.', [{'summary':'Встреча перенесена на пятницу.', 'evidence':'Встреча будет в пятницу.'}])

    def test_deadline_in_condition_is_preserved(self):
        with self.assertRaises(ValueError):
            self.render('Если склад подтвердит наличие до четверга, отправим товар в пятницу.', [{'summary':'Товар отправят в пятницу, если склад подтвердит наличие.', 'evidence':'Если склад подтвердит наличие до четверга, отправим товар в пятницу.'}])

    def test_explicit_without_confirmation_supports_causal_paraphrase(self):
        self.assertEqual(self.render('Без подтверждения дату отправки не обещаем.', [{'summary':'Дату отправки не обещают из-за отсутствия подтверждения.', 'evidence':'Без подтверждения дату отправки не обещаем.'}]), '• Дату отправки не обещают из-за отсутствия подтверждения.')

    def test_unknown_date_preserves_absence_without_separate_not_word(self):
        self.assertEqual(self.render('Поставщик сообщил о задержке, новой даты пока нет.', [{'summary':'Поставку задержали, новая дата неизвестна.', 'evidence':'Поставщик сообщил о задержке, новой даты пока нет.'}]), '• Поставку задержали, новая дата неизвестна.')

    def test_colon_explaining_absence_supports_causal_paraphrase(self):
        self.assertEqual(self.render('Нужно обсудить прототип завтра. В работу его пока не берём: оценки трудозатрат нет.', [{'summary':'В работу прототип пока не берут из-за отсутствия оценок.', 'evidence':'В работу его пока не берём: оценки трудозатрат нет.'}]), '• В работу прототип пока не берут из-за отсутствия оценок.')


if __name__ == '__main__':
    unittest.main()
