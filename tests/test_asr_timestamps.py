import sys,unittest
from pathlib import Path
from types import SimpleNamespace as S
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import worker

class AlignedTimestampTests(unittest.TestCase):
 def test_token_starts_map_to_audio_window_and_pause_splits(self):
  result=S(tokens=[S(text='Первая',start=2.,duration=.4),S(text=' реплика.',start=2.5,duration=.4),
                   S(text='Вторая',start=32.,duration=.4),S(text=' реплика',start=32.5,duration=.4)])
  self.assertEqual(worker.aligned_segments(result,1000.,45.),
                   [(1002.,'Первая реплика.'),(1032.,'Вторая реплика')])

 def test_unpunctuated_speech_is_split_on_actual_pause(self):
  result=S(tokens=[S(text='Первый вопрос',start=1.,duration=1.),S(text=' второй вопрос',start=20.,duration=1.)])
  self.assertEqual(worker.aligned_segments(result,100.,45.),[(101.,'Первый вопрос'),(120.,'второй вопрос')])

 def test_plain_text_legacy_result_does_not_invent_alignment(self):
  self.assertIsNone(worker.aligned_segments(S(text='Реплика'),100.,45.))

 def test_punctuation_only_token_group_is_not_an_utterance(self):
  result=S(tokens=[S(text='Реплика.',start=1.,duration=1.),S(text='.',start=20.,duration=.1)])
  self.assertEqual(worker.aligned_segments(result,100.,45.),[(101.,'Реплика.')])
