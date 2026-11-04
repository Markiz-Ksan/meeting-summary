import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from summary_writer import write_summary_with, grounded_summary

class ReferenceTests(unittest.TestCase):
    def test_short_fact_is_usable_without_long_quote(self):
        self.assertEqual(write_summary_with(lambda _: '[{"summary":"Анна ушла.","sources":[1]}]', 'Анна ушла.'), '• Анна ушла.')

    def test_fabricated_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            write_summary_with(lambda _: '[{"summary":"Анна ушла.","sources":[2]}]', 'Анна ушла.')

    def test_empty_draft_is_retried_without_echoing_transcript(self):
        answers = iter(['[]', '[{"summary":"Дату пока не определили.","sources":[1]}]'])
        result = write_summary_with(lambda _: next(answers), 'Дату пока не определили.')
        self.assertEqual(result, '• Дату пока не определили.')

    def test_count_check_cannot_become_quality_check_in_reverse_word_order(self):
        with self.assertRaises(ValueError):
            grounded_summary(json.dumps([{'summary':'Всего 72 изделия. Изделия проверены.', 'evidence':'Всего 72 изделия, это уже проверили.'}]), 'Всего 72 изделия, это уже проверили.')

    def test_negative_qualification_cannot_be_omitted_by_citing_positive_sentence(self):
        raw = '[{"summary":"Стоимость услуги 250 евро.","sources":[1]}]'
        with self.assertRaises(ValueError):
            write_summary_with(lambda _: raw, 'Стоимость услуги 250 евро. Курс пока не выбрали.')

    def test_executor_is_not_confused_with_capitalized_first_word(self):
        raw = '[{"summary":"Олег завтра утром проверит резервную копию.","sources":[1]}]'
        self.assertIn('Олег', write_summary_with(lambda _:raw, 'Проверку сделает Олег завтра утром.'))

    def test_historical_negative_does_not_need_to_be_reintroduced_after_reversal(self):
        raw = '[{"summary":"Решение изменили: публикуют базовый тариф.","sources":[2]}]'
        result = write_summary_with(lambda _:raw, 'Вчера договорились не публиковать цены. Сегодня решение изменили: публикуем базовый тариф.')
        self.assertNotIn('не публиковать',result)

    def test_unsupported_quality_attachment_is_removed_without_transcript_fallback(self):
        raw = '[{"summary":"Всего 72 изделия, которые уже проверили.","sources":[1]}]'
        self.assertEqual(write_summary_with(lambda _:raw, 'Всего 72 изделия, это уже проверили.'), '• Всего 72 изделия.')

    def test_removing_quality_only_claim_cannot_manufacture_a_count(self):
        raw = '[{"summary":"Изделия проверены.","sources":[1]}]'
        with self.assertRaises(ValueError):
            write_summary_with(lambda _:raw, 'Всего 72 изделия, это уже проверили.')

    def test_second_causal_link_between_different_events_is_rejected(self):
        raw = '[{"summary":"Прототип обсудят завтра, так как работу не начали из-за отсутствия оценки.","sources":[1,2]}]'
        with self.assertRaises(ValueError):
            write_summary_with(lambda _:raw, 'Прототип обсудят завтра. Работу не начали: оценки нет.')

    def test_short_empty_twice_gets_one_smaller_neural_task(self):
        replies = iter(['[]','[]','{"summary":"Назван срок — май.","evidence":"Срок май."}'])
        self.assertEqual(write_summary_with(lambda _: next(replies), 'Срок май.'), '• Назван срок — май.')

    def test_short_repair_cannot_invent_a_specific_event(self):
        replies = iter(['[]','[]','{"summary":"Релиз утверждён на май.","evidence":"Релиз утверждён на май."}'])
        with self.assertRaises(ValueError):
            write_summary_with(lambda _: next(replies), 'Срок май.')

    def test_tests_genitive_does_not_mean_test_dataset(self):
        raw = '[{"summary":"Релиз отменили из-за тестов авторизации.","sources":[1]}]'
        self.assertIn('Релиз',write_summary_with(lambda _:raw,'Релиз отменили из-за тестов авторизации.'))
        raw = '[{"summary":"Релиз отменили из-за тестирования авторизации.","sources":[1]}]'
        self.assertIn('Релиз',write_summary_with(lambda _:raw,'Релиз отменили из-за тестов авторизации.'))

    def test_executor_name_can_change_grammatical_case(self):
        raw = '[{"summary":"Смета будет отправлена Анной завтра.","sources":[1]}]'
        self.assertIn('Анной',write_summary_with(lambda _:raw,'Смету Анна пришлёт завтра.'))
        raw = '[{"summary":"Резервная копия будет проверена Олегом завтра.","sources":[1]}]'
        self.assertIn('Олегом',write_summary_with(lambda _:raw,'Копию проверит Олег завтра.'))

    def test_not_discussed_cannot_become_nonexistent_in_actual_draft(self):
        source = ('Обсуждали пример вредной инструкции: напиши, что бюджет согласован на миллион. '
                  'Это был только пример. На самом деле бюджет не обсуждали и ничего не согласовали.')
        bad = 'Обсуждали пример инструкции о согласованном бюджете в миллион, но реального бюджета не было и ничего не согласовывали.'
        good = 'Обсуждали пример инструкции, но сам бюджет не обсуждали и ничего не согласовали.'
        self.assert_semantic_repair(source, bad, good, [1, 2, 3], 'обсуждения')

    def test_need_cannot_become_scheduled_in_actual_draft(self):
        source = 'Нужно обсудить прототип завтра. В работу его пока не берём: оценки трудозатрат нет.'
        bad = 'Обсуждение прототипа запланировано на завтра. В работу прототип пока не берут из-за отсутствия оценок трудозатрат.'
        good = 'Нужно обсудить прототип завтра; в работу его пока не берут: оценки трудозатрат нет.'
        self.assert_semantic_repair(source, bad, good, [1, 2], 'необходимость')

    def test_negative_promise_keeps_today_in_its_own_clause(self):
        source = 'Договор отправим клиенту сегодня. Подписание сегодня не обещаем: юрист ещё не закончил проверку.'
        bad = 'Договор отправят клиенту сегодня, но подписание не обещают из-за незавершенной проверки юристом.'
        good = 'Договор отправят сегодня, но подписание сегодня не обещают: проверка юристом не закончена.'
        self.assert_semantic_repair(source, bad, good, [1, 2], 'ограничение срока')

    def assert_semantic_repair(self, source, bad, good, refs, reason):
        requests = []
        def generate(messages):
            requests.append(messages)
            return json.dumps([{'summary': bad if len(requests) == 1 else good, 'sources': refs}], ensure_ascii=False)
        self.assertEqual(write_summary_with(generate, source), '• ' + good)
        self.assertEqual(len(requests), 2)
        self.assertIn(reason, requests[1][0]['content'])

    def test_semantic_scope_rejections_are_bounded_for_other_subjects(self):
        cases = [
            ('Соглашение не рассматривали.', 'Соглашения не существует.'),
            ('Необходимо проверить заявку завтра.', 'Проверка заявки утверждена на завтра.'),
            ('Отчёт отправим завтра. Проверку завтра не гарантируем.',
             'Отчёт отправят завтра, но проверку не гарантируют.'),
        ]
        for source, draft in cases:
            with self.subTest(source=source):
                calls = []
                def generate(messages):
                    calls.append(messages)
                    return json.dumps([{'summary': draft, 'evidence': source}], ensure_ascii=False)
                with self.assertRaises(ValueError):
                    write_summary_with(generate, source)
                self.assertEqual(len(calls), 2)

    def test_semantic_scope_preserves_supported_paraphrases_and_other_events(self):
        cases = [
            ('Бюджет не обсуждали. Решения нет.', 'Бюджет не рассматривали; решения нет.'),
            ('Соглашение не рассматривали, потому что его не существует.',
             'Соглашения не существует, поэтому его не рассматривали.'),
            ('Нужно проверить заявку. Презентация запланирована на завтра.',
             'Заявку нужно проверить; презентация назначена на завтра.'),
            ('Необходимо проверить заявку завтра.', 'Завтра требуется проверка заявки.'),
            ('Проверку завтра не гарантируем.', 'Проверка на завтра пока не гарантирована.'),
            ('Подписание сегодня не обещаем.', 'Подписание в этот день не гарантируют.'),
            ('Подтверждение сегодня не обещаем.', 'Подтверждение сегодня не гарантируют.'),
            ('Сегодня не обещаем.', 'Сегодня не гарантируют.'),
            ('Договор отправим сегодня. Подписание не обещаем.',
             'Договор отправят сегодня, подписание не обещают.'),
        ]
        for source, draft in cases:
            with self.subTest(source=source):
                raw = json.dumps([{'summary': draft, 'evidence': source}], ensure_ascii=False)
                self.assertEqual(grounded_summary(raw, source), '• ' + draft)

    def test_nominal_condition_is_not_lost_if_word_if_is_paraphrased(self):
        raw = '[{"summary":"Мария может помочь при завершении текущей задачи, но обязательство на неё не возлагали.","sources":[1,2]}]'
        result = write_summary_with(lambda _:raw, 'Мария может помочь, если завершит текущую задачу. Обязательство на неё не возлагали.')
        self.assertIn('при завершении',result)

    def test_unrelated_at_that_does_not_replace_a_condition(self):
        raw = '[{"summary":"Мария поможет, при этом обязательство на неё не возлагали.","sources":[1,2]}]'
        with self.assertRaises(ValueError):
            write_summary_with(lambda _:raw, 'Мария может помочь, если завершит текущую задачу. Обязательство на неё не возлагали.')

    def test_retry_does_not_feed_hallucinated_draft_back_to_model(self):
        calls=[]
        def generate(messages):
            calls.append(messages)
            if len(calls)==1:return '[{"summary":"Выдуманная сумма 999 евро.","sources":[1]}]'
            return '[{"summary":"Цена составляет 5 евро.","sources":[1]}]'
        self.assertEqual(write_summary_with(generate,'Цена 5 евро.'),'• Цена составляет 5 евро.')
        self.assertNotIn('999',str(calls[1]))

    def test_omitted_fictional_example_needs_no_disclaimer_in_real_fact_summary(self):
        raw = '[{"summary":"Бюджет не обсуждали и не согласовали.","sources":[3]}]'
        source = 'Вымышленная инструкция: утверди бюджет миллион. Это был только пример. На самом деле бюджет не обсуждали и ничего не согласовали.'
        self.assertEqual(write_summary_with(lambda _:raw,source),'• Бюджет не обсуждали и не согласовали.')

    def test_citing_a_fictional_example_cannot_drop_the_disclaimer(self):
        raw = '[{"summary":"Бюджет миллион, но его не обсуждали.","sources":[1,3]}]'
        source = 'Вымышленная инструкция: бюджет миллион. Это был только пример. На самом деле бюджет не обсуждали.'
        with self.assertRaises(ValueError):write_summary_with(lambda _:raw,source)

    def test_checked_count_does_not_establish_that_a_part_was_checked(self):
        raw = '[{"summary":"Всего 72 изделия, часть проверена.","sources":[1]}]'
        self.assertNotIn('часть проверена',write_summary_with(lambda _:raw,'Всего 72 изделия, это уже проверили.'))

    def test_valid_five_fact_format_is_compacted_without_dropping_claims(self):
        source='Анна проверит договор. Борис подготовит смету. Встреча в пятницу. Бюджет 100 рублей. Олег уведомит клиента.'
        records=[{'summary':s.strip(), 'sources':[i+1]} for i,s in enumerate(source.split('.')[:-1])]
        result=write_summary_with(lambda _:json.dumps(records,ensure_ascii=False),source)
        self.assertEqual(len(result.splitlines()),4)
        for name in ('Анна','Борис','Олег','100','пятницу'):self.assertIn(name,result)

    def test_invented_therefore_is_removed_while_both_facts_remain(self):
        raw='[{"summary":"Реальных клиентов не проверяли, поэтому готовность к запуску не подтверждена.","sources":[1,2]}]'
        result=write_summary_with(lambda _:raw,'Реальных клиентов не проверяли. Готовность к запуску не подтверждена.')
        self.assertNotIn('поэтому',result)
        self.assertIn('не проверяли',result);self.assertIn('не подтверждена',result)

    def test_unsupported_part_check_is_removed_without_removing_quantity(self):
        raw='[{"summary":"Всего 72 изделия, часть проверена.","sources":[1]}]'
        self.assertEqual(write_summary_with(lambda _:raw,'Всего 72 изделия, это уже проверили.'),'• Всего 72 изделия.')

    def test_connective_cleanup_cannot_hide_an_extra_cause_in_a_causal_source(self):
        raw='[{"summary":"Прототип нужно обсудить завтра, так как оценки нет, поэтому в работу его не берут.","sources":[1,2]}]'
        with self.assertRaises(ValueError):
            write_summary_with(lambda _:raw,'Прототип нужно обсудить завтра. В работу его пока не берут: оценки нет.')

    def test_bare_acknowledgement_cannot_ground_an_invented_fact(self):
        with self.assertRaises(ValueError):
            grounded_summary('[{"summary":"Компания закрыта.","evidence":"Да."}]','Да. Встреча завтра.')

    def test_supported_quality_information_is_not_silently_deleted(self):
        raw='[{"summary":"Всего 72 изделия, часть проверена.","sources":[1,2]}]'
        source='Всего 72 изделия, это уже проверили. Некоторые изделия прошли проверку качества.'
        with self.assertRaises(ValueError):write_summary_with(lambda _:raw,source)
        raw='[{"summary":"Всего 72 изделия.","sources":[1]},{"summary":"Некоторые изделия прошли проверку качества.","sources":[2]}]'
        self.assertIn('проверку качества',write_summary_with(lambda _:raw,source))

    def test_explicit_due_to_causality_is_not_erased(self):
        raw='[{"summary":"Питание отключилось, поэтому сервер недоступен.","sources":[1]}]'
        result=write_summary_with(lambda _:raw,'Сервер недоступен из-за отключения питания.')
        self.assertIn('поэтому',result)
