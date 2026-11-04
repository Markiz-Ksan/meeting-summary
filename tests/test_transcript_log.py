import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from transcript_log import TranscriptLog


class TranscriptLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'Встреча.txt'
        self.log = TranscriptLog(self.path)

    def test_overlap_correction_and_unfinished_phrase(self):
        self.log.update('Встречу переносим на пятницу. Бюджет составит сто тысяч рублей.', 6, 6)
        self.log.update('Встречу переносим на пятницу. Бюджет составит двести тысяч рублей. Анна проверит', 12, 12)
        self.log.update('Встречу переносим на пятницу. Бюджет составит двести тысяч рублей. Анна проверит смету к вечеру.', 18, 18)
        text = self.path.read_text()
        self.assertEqual(text.count('Встречу переносим'), 1)
        self.assertNotIn('составит сто тысяч', text)
        self.assertEqual(text.count('Анна проверит'), 1)
        self.assertIn('Анна проверит смету к вечеру.', text)

    def test_tail_is_saved_without_final_punctuation(self):
        self.log.update('Анна проверит смету', 6, 6)
        self.assertIn('Анна проверит смету', self.path.read_text())

    def test_full_meeting_survives_five_minutes_and_300_entries(self):
        for i in range(305):
            self.log.update(f'Решение номер {i} по отдельному вопросу.', 60*i+6, 6)
        text = self.path.read_text()
        self.assertIn('Решение номер 0 ', text)
        self.assertIn('Решение номер 304 ', text)
        self.assertEqual(text.count('Решение номер'), 305)
        self.assertEqual(list(self.path.parent.glob('.transcript-*')), [])

    def test_silence_does_not_erase_and_new_session_does_not_overwrite(self):
        self.log.update('Бюджет согласован.', 6, 6)
        before = self.path.read_bytes()
        self.log.update('', 1000, 6)
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(FileExistsError):
            TranscriptLog(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

class TranscriptLossRegressionTests(unittest.TestCase):
    def test_twenty_minute_session_with_disjoint_decodes_keeps_beginning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            log = TranscriptLog(path)
            for index in range(200):
                # Changing recognition results used to erase the previous 45 s
                # repeatedly, leaving only the last few lines of a long call.
                text = ['Бюджет утверждён.', 'Сегодня поступил договор.', 'Клиент ждёт доставки.', 'Проверку выполнят завтра.'][index % 4]
                log.update(text, 6 * (index + 1), min(45, 6 * (index + 1)))
            self.assertEqual(log.history.entries[0][0],6)

class TranscriptRestartTests(unittest.TestCase):
    def test_aligned_starts_are_saved_and_checkpoint_still_uses_chunk_identity(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            base = datetime(2026,11,4,12,0,0).timestamp()
            log = TranscriptLog(path)
            log.update('Первая реплика. Вторая реплика.',base+45,45,request_id=8,
                       segments=[(base+2,'Первая реплика.'),(base+32,'Вторая реплика.')])
            self.assertIn('[12:00:02] Первая реплика.',path.read_text())
            self.assertIn('[12:00:32] Вторая реплика.',path.read_text())
            restored = TranscriptLog(path,resume=True)
            self.assertTrue(restored.was_committed(8,base+45))
            self.assertEqual(restored.history.entries,[(base+2,'Первая реплика.'),(base+32,'Вторая реплика.')])

    def test_audio_reference_and_zero_time_survive_resume(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            audio = Path(directory) / 'audio/session.wav'
            stamp = datetime(2026, 11, 4, 12, 0, 0).timestamp()
            log = TranscriptLog(path, audio_path=audio, audio_started_at=stamp)
            log.update('Тестовая реплика.', stamp+6, 6, request_id=1)
            resumed = TranscriptLog(path, resume=True)
            resumed.update('Следующая реплика.', stamp+12, 6, request_id=2)
            content = path.read_text()
            self.assertIn('Аудиозапись: ' + str(audio), content)
            self.assertIn('Начало аудиозаписи (00:00:00): 2026-11-04T12:00:00', content)
            self.assertEqual(content.count('Аудиозапись:'), 1)
            self.assertIn('Тестовая реплика.', content)
            self.assertIn('Следующая реплика.', content)

    def test_resume_and_replay_after_lost_ack_is_idempotent(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            timestamp = datetime.now().timestamp()
            original = TranscriptLog(path)
            original.update('Бюджет пока не утверждён. Анна проверит смету.', timestamp, 6)
            before = path.read_bytes()
            resumed = TranscriptLog(path, resume=True)
            resumed.update('Бюджет пока не утверждён. Анна проверит смету.', timestamp, 6)
            self.assertEqual(path.read_bytes(), before)
            resumed.update('Бюджет пока не утверждён. Анна проверит смету. Договор отправят завтра.', timestamp+6, 12)
            after = path.read_text()
            self.assertEqual(after.count('Бюджет пока не утверждён.'),1)
            self.assertIn('Договор отправят завтра.',after)

    def test_failed_replace_keeps_committed_file_and_retry_retains_new_text(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            log = TranscriptLog(path)
            log.update('Бюджет пока не утверждён.',6,6)
            before = path.read_bytes()
            with patch('transcript_log.os.replace',side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    log.update('Бюджет пока не утверждён. Анна проверит смету.',12,12)
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(list(path.parent.glob('.transcript-*')),[])
            log.update('Бюджет пока не утверждён. Анна проверит смету.',12,12)
            after = path.read_text()
            self.assertEqual(after.count('Бюджет пока не утверждён.'),1)
            self.assertEqual(after.count('Анна проверит смету.'),1)

    def test_resume_across_midnight_restores_chronological_order(self):
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            log = TranscriptLog(path)
            first = datetime.now().replace(hour=23,minute=59,second=58,microsecond=0)
            log.update('Первая реплика перед полуночью.',first.timestamp(),6)
            log.update('Вторая реплика после полуночи.',(first+timedelta(seconds=8)).timestamp(),6)
            restored = TranscriptLog(path,resume=True)
            self.assertEqual(restored.history.entries[1][0]-restored.history.entries[0][0],8)

if __name__ == '__main__':
    unittest.main()

class RecordingStartDateTests(unittest.TestCase):
    def test_worker_starting_after_midnight_uses_recording_start_day(self):
        from datetime import datetime
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'session.txt'
            started=datetime(2026,11,4,23,59,58).timestamp()
            log=TranscriptLog(path,audio_path=Path(tmp)/'session.wav',audio_started_at=started)
            log.update('Первая фраза.',started+1,1,segments=[(started+1,'Первая фраза.')])
            log.update('Вторая фраза.',started+4,4,segments=[(started+4,'Вторая фраза.')])
            # Simulate the older misleading creation date while preserving the
            # authoritative audio-zero anchor and midnight-spanning clock rows.
            text=path.read_text();text=text.replace('Начало: 2026-11-04','Начало: 2026-11-05')
            path.write_text(text)
            restored=TranscriptLog(path,resume=True)
            self.assertEqual([stamp for stamp,_ in restored.history.entries],[started+1,started+4])
            self.assertEqual(log.header.splitlines()[1][:18],'Начало: 2026-11-04')
