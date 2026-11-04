import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from session_title import generate_title, topic_context

class TitleTests(unittest.TestCase):
    def test_neutral_title_with_source(self):
        source = 'Обсудили сроки поставки оборудования. Срок ещё не подтверждён.'
        result = generate_title(lambda _: json.dumps({'title':'Сроки поставки оборудования', 'evidence':'Обсудили сроки поставки оборудования.'}, ensure_ascii=False), source)
        self.assertEqual(result, 'Сроки поставки оборудования')

    def test_unsupported_numbers_and_decision_are_rejected(self):
        source = 'Обсудили поставку оборудования. Дата неизвестна.'
        for title in ('Поставка 500 новых станков', 'Успешная поставка нового оборудования'):
            self.assertIsNone(generate_title(lambda _: json.dumps({'title':title, 'evidence':source}), source))

    def test_invalid_or_empty_generation_has_safe_fallback(self):
        for raw in ('', '[]', '{}', 'не json'):
            self.assertIsNone(generate_title(lambda _:raw, 'Обсудили сроки поставки оборудования.'))

    def test_sampling_keeps_beginning_middle_end_and_is_bounded(self):
        text = 'НАЧАЛО ' + 'а'*18000 + ' СЕРЕДИНА ' + 'б'*18000 + ' КОНЕЦ'
        context = topic_context(text)
        self.assertTrue(context.startswith('НАЧАЛО'))
        self.assertTrue(context.endswith('КОНЕЦ'))
        self.assertLess(len(context), 12200)

    def test_title_cannot_apply_a_change_to_two_different_events(self):
        source='Встречу переносим на пятницу. Релиз пока не выпускаем.'
        self.assertIsNone(generate_title(lambda _:json.dumps({'title':'Перенос встречи и релиза','evidence':source}),source))
