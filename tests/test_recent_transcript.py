import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from recent_transcript import RecentTranscript

class RecentTests(unittest.TestCase):
 def test_recovered_prefix_is_kept(self):
  r=RecentTranscript()
  r.update('Анна подготовит смету к вечеру.',6,6)
  r.update('Встречу переносим с четверга на пятницу. Анна подготовит смету к вечеру. Бюджет остаётся сто тысяч рублей.',12,12)
  self.assertIn('Встречу переносим',r.text)
  self.assertEqual(len(r.entries),3)
 def test_cut_off_prefix_is_not_duplicated(self):
  r=RecentTranscript()
  r.update('Встречу переносим с четверга на пятницу. Анна подготовит смету к вечеру.',40,40)
  r.update('с четверга на пятницу. Анна подготовит смету к вечеру. Бюджет остаётся сто тысяч рублей.',50,45)
  self.assertEqual(r.text.count('с четверга на пятницу.'),1)
 def test_overlap_and_correction(self):
  r=RecentTranscript()
  r.update('Бюджет проекта сто тысяч рублей. Релиз сегодня не выпускаем.',6,6)
  r.update('Бюджет проекта сто тысяч рублей. Релиз сегодня не выпускаем. Анна проверит смету.',12,12)
  self.assertEqual(len(r.entries),3)
  r.update('Релиз сегодня не выпускаем. Анна проверит смету. Новую дату релиза пока не назначили.',52,45)
  self.assertEqual(r.text.count('Анна проверит смету.'),1)
  self.assertIn('Бюджет проекта сто тысяч рублей.',r.text)
  self.assertIn('Новую дату релиза пока не назначили.',r.text)
 def test_five_minutes_and_silence(self):
  r=RecentTranscript()
  r.update('Согласовали первоначальный бюджет проекта.',6,6)
  r.update('Следующий этап запускаем в пятницу.',180,6)
  self.assertIn('первоначальный',r.text)
  r.update('Уточнённый бюджет проекта утверждён.',312,6)
  self.assertNotIn('первоначальный',r.text)
  self.assertIn('Следующий этап',r.text)
  self.assertEqual(r.update('',613,6),'')
 def test_same_fact_later_is_not_dropped(self):
  r=RecentTranscript()
  r.update('Релиз выпускаем в пятницу.',6,6)
  r.update('Релиз выпускаем в понедельник.',120,6)
  self.assertEqual(len(r.entries),2)
 def test_thirty_minute_session_keeps_only_recent_text(self):
  r=RecentTranscript()
  for i in range(31):
   r.update(f'Обсуждаем этап номер {i} и отдельное решение по нему.',i*60+6,6)
  self.assertEqual(len(r.entries),6)
  self.assertNotIn('номер 0 ',r.text)
  self.assertIn('номер 30 ',r.text)

class MissingSpeechRegressionTests(unittest.TestCase):
 def test_late_recovered_speech_keeps_audio_start_not_current_chunk_time(self):
  r=RecentTranscript()
  r.update('Кодовое слово водопад.',16,16,segments=[(8.1,'Кодовое слово водопад.')])
  r.update('Кодовое слово водопад. Кодовое слово дельфин.',46,45,
           segments=[(8.2,'Кодовое слово водопад.'),(38.1,'Кодовое слово дельфин.')])
  self.assertEqual([round(t,1) for t,_ in r.entries],[8.1,38.1])

 def test_similar_later_phrase_cannot_relabel_earlier_speech(self):
  r=RecentTranscript()
  r.update('Контрольная точка один.',16,16,segments=[(8.,'Контрольная точка один.')])
  r.update('Контрольная точка два.',46,45,segments=[(38.,'Контрольная точка два.')])
  self.assertEqual(r.entries,[(8.,'Контрольная точка один.'),(38.,'Контрольная точка два.')])

 def test_a_decode_with_no_shared_sentences_never_erases_history(self):
  r=RecentTranscript()
  r.update('Утвердили бюджет проекта. Клиент получил договор.',6,6)
  r.update('Завтра начинается монтаж оборудования.',12,12)
  self.assertEqual(len(r.entries),3)
  self.assertIn('Утвердили бюджет',r.text)

 def test_omitted_middle_and_tail_are_preserved(self):
  r=RecentTranscript()
  r.update('Утвердили бюджет проекта. Клиент получил договор. Доставку перенесли. Анна проверит счета.',30,30)
  r.update('Утвердили бюджет проекта. Доставку перенесли.',36,36)
  self.assertIn('Клиент получил договор.',r.text)
  self.assertIn('Анна проверит счета.',r.text)
  self.assertEqual(len(r.entries),4)

 def test_shortened_decode_does_not_replace_completed_sentence(self):
  r=RecentTranscript()
  r.update('Анна проверит смету к вечеру.',6,6)
  r.update('Анна проверит',12,12)
  self.assertEqual(r.text,'Анна проверит смету к вечеру.')

 def test_recovered_prefix_keeps_chronological_timestamps(self):
  r=RecentTranscript()
  r.update('Анна проверит смету.',6,6)
  r.update('Утвердили бюджет. Анна проверит смету. Договор отправлен.',12,12)
  self.assertEqual([stamp for stamp,_ in r.entries],sorted(stamp for stamp,_ in r.entries))

 def test_repeated_phrase_is_not_globally_deduplicated(self):
  r=RecentTranscript()
  r.update('Согласовано.',6,6)
  r.update('Согласовано. Согласовано.',12,12)
  self.assertEqual(len(r.entries),2)

if __name__=='__main__': unittest.main()

class TimedFragmentTests(unittest.TestCase):
 def test_inflected_partial_prefix_is_replaced_by_full_phrase(self):
  r=RecentTranscript()
  r.update('Контрольная точка два кодовое слово.',12,12,segments=[(8.0,'Контрольная точка два кодовое слово.')])
  r.update('Контрольная точка два кодовые слова зелёный дельфин.',18,18,segments=[(8.1,'Контрольная точка два кодовые слова зелёный дельфин.')])
  self.assertEqual(r.entries,[(8.0,'Контрольная точка два кодовые слова зелёный дельфин.')])
 def test_clipped_variant_never_shrinks_saved_complete_phrase(self):
  r=RecentTranscript()
  r.update('Контрольная точка два кодовые слова зелёный дельфин.',12,12,segments=[(8.,'Контрольная точка два кодовые слова зелёный дельфин.')])
  r.update('Контрольная точка два кодовое слово.',18,18,segments=[(8.1,'Контрольная точка два кодовое слово.')])
  self.assertIn('зелёный дельфин',r.text)
  self.assertEqual(len(r.entries),1)
 def test_similar_fragments_at_different_times_are_distinct(self):
  r=RecentTranscript()
  r.update('Контрольная точка два кодовое слово.',12,12,segments=[(8.,'Контрольная точка два кодовое слово.')])
  r.update('Контрольная точка два кодовые слова зелёный дельфин.',24,24,segments=[(20.,'Контрольная точка два кодовые слова зелёный дельфин.')])
  self.assertEqual(len(r.entries),2)

class CriticalDifferenceTests(unittest.TestCase):
 def test_close_opposite_utterances_are_not_erased(self):
  r=RecentTranscript()
  r.update('Доставку обещаем сегодня.',12,12,segments=[(8.,'Доставку обещаем сегодня.')])
  r.update('Доставку не обещаем сегодня.',18,18,segments=[(9.,'Доставку не обещаем сегодня.')])
  self.assertEqual(len(r.entries),2)
 def test_close_different_amounts_are_not_erased(self):
  r=RecentTranscript()
  r.update('Стоимость услуги 100 рублей.',12,12,segments=[(8.,'Стоимость услуги 100 рублей.')])
  r.update('Стоимость услуги 200 рублей.',18,18,segments=[(9.,'Стоимость услуги 200 рублей.')])
  self.assertEqual(len(r.entries),2)
 def test_close_different_deadlines_are_not_erased(self):
  r=RecentTranscript()
  r.update('Отчёт отправим сегодня.',12,12,segments=[(8.,'Отчёт отправим сегодня.')])
  r.update('Отчёт отправим завтра.',18,18,segments=[(9.,'Отчёт отправим завтра.')])
  self.assertEqual(len(r.entries),2)
 def test_extending_a_clipped_prefix_can_add_a_negation(self):
  r=RecentTranscript()
  r.update('Сегодня доставку',12,12,segments=[(8.,'Сегодня доставку')])
  r.update('Сегодня доставку не обещаем.',18,18,segments=[(8.1,'Сегодня доставку не обещаем.')])
  self.assertEqual(r.text,'Сегодня доставку не обещаем.')
