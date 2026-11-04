"""Pipeline regressions run without loading MLX/models or recording user audio."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import worker
from recent_transcript import RecentTranscript
from transcript_log import TranscriptLog
from collections import deque


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name)
        self.wav = self.directory / 'chunk.wav'
        self.wav.write_bytes(b'synthetic')

    def run_worker(self, mode, request, engine):
        output = io.StringIO()
        argv = ['worker', '--mode', mode, '--session', str(self.directory)]
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO(json.dumps(request)+'\n')), patch.object(worker, 'Engine', return_value=engine), contextlib.redirect_stdout(output):
            self.assertEqual(worker.main(), 0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_asr_ack_keeps_audio_and_echoes_request_identity(self):
        def process(path, timestamp, **options):
            self.assertEqual(path, self.wav.resolve())
            self.assertEqual(options, {'summarize': False, 'keep_audio': True, 'request_id':42})
            return {'summary_context': 'Согласовали перенос встречи.', 'context_timestamp': timestamp, 'summary': ''}
        events = self.run_worker('transcribe', {'request_id': 42, 'path': str(self.wav), 'timestamp': 123.5}, SimpleNamespace(process=process))
        self.assertTrue(self.wav.exists())
        self.assertEqual(events[-1]['event'], 'result')
        self.assertEqual(events[-1]['request_id'], 42)
        self.assertEqual(events[-1]['context_timestamp'], 123.5)

    def test_audio_metadata_arguments_reach_persistent_transcript_without_model_load(self):
        output = io.StringIO()
        transcript = self.directory / 'session.txt'
        audio = self.directory / 'saved recording.wav'
        argv = ['worker', '--mode', 'transcribe', '--session', str(self.directory),
                '--transcript-file', str(transcript), '--audio-file', str(audio), '--audio-start', '1700000000.25']
        with patch.object(sys, 'argv', argv), patch.object(sys, 'stdin', io.StringIO('')), \
                patch.object(worker, 'Engine', return_value=SimpleNamespace()), contextlib.redirect_stdout(output):
            self.assertEqual(worker.main(), 0)
        self.assertIn('Аудиозапись: ' + str(audio), transcript.read_text())
        self.assertIn('Начало аудиозаписи (00:00:00): ', transcript.read_text())
        self.assertEqual(json.loads(output.getvalue())['event'], 'ready')

    def test_archive_failure_is_retryable_and_never_acks_audio(self):
        def process(*args, **kwargs):
            raise worker.ArchiveWriteError('disk full')
        events = self.run_worker('transcribe', {'request_id': 'c7', 'path': str(self.wav)}, SimpleNamespace(process=process))
        self.assertTrue(self.wav.exists())
        self.assertEqual(events[-1]['event'], 'error')
        self.assertEqual(events[-1]['stage'], 'archive')
        self.assertEqual(events[-1]['request_id'], 'c7')
        self.assertTrue(events[-1]['retryable'])
        self.assertFalse(any(e['event']=='result' for e in events))

    def test_summary_result_has_source_timestamp_and_identity(self):
        engine = SimpleNamespace(summarize=lambda text: '• ' + text, mx=SimpleNamespace(get_active_memory=lambda: 0))
        events = self.run_worker('summarize', {'text': 'Срок пока не утверждён.', 'timestamp': 400, 'request_id': 8}, engine)
        self.assertEqual(events[-1]['summary'], '• Срок пока не утверждён.')
        self.assertEqual(events[-1]['context_timestamp'], 400)
        self.assertEqual(events[-1]['request_id'], 8)

    def test_invalid_summary_keeps_worker_alive_and_preserves_request_identity(self):
        def reject(text): raise ValueError('Unsupported evidence')
        engine = SimpleNamespace(summarize=reject, mx=SimpleNamespace(get_active_memory=lambda:0))
        events = self.run_worker('summarize', {'text':'Решение не принято.', 'timestamp':400, 'request_id':8}, engine)
        self.assertEqual(events[-1]['event'], 'result')
        self.assertEqual(events[-1]['summary'], '')
        self.assertEqual(events[-1]['request_id'], 8)
        self.assertTrue(events[-1]['validation_failed'])

    def test_outside_path_is_rejected_without_deletion(self):
        sibling = self.directory / 'nested'
        sibling.mkdir()
        external = sibling / 'chunk.wav'
        external.write_bytes(b'audio')
        events = self.run_worker('transcribe', {'path': str(external), 'request_id': 9}, SimpleNamespace())
        self.assertEqual(events[-1]['event'], 'error')
        self.assertTrue(external.exists())

    def test_unpunctuated_context_is_strictly_bounded(self):
        engine = worker.Engine.__new__(worker.Engine)
        engine.model = engine.tokenizer = engine.sampler = None
        engine.mx = SimpleNamespace(clear_cache=lambda: None)
        captured = []
        def summarize(model, tokenizer, text, sampler):
            captured.append(text)
            return '• Содержательная сводка последних реплик.'
        with patch('summary_writer.write_summary', side_effect=summarize):
            engine.summarize('длинная речь без пунктуации ' * 3000)
        self.assertEqual(len(captured), 1)
        self.assertLessEqual(len(captured[0]), 10000)

    def test_continuous_speech_includes_only_sentence_and_unfinished_tail(self):
        engine = worker.Engine.__new__(worker.Engine)
        engine.audio = [0]*6
        engine.speech = SimpleNamespace(preprocessor_config=SimpleNamespace(sample_rate=1))
        engine.transcript_log = None
        engine.mx = SimpleNamespace(get_active_memory=lambda: 0)
        engine.recent = RecentTranscript()
        engine.history = deque()
        engine.tail_has_speech = True
        engine.transcribe = lambda *args: 'Встречу перенесли на пятницу. Анна проверит'
        result = engine.process(self.wav, 6, summarize=False, keep_audio=True)
        self.assertEqual(result['summary_context'], 'Встречу перенесли на пятницу. Анна проверит')
        self.assertTrue(self.wav.exists())

    def test_process_passes_aligned_audio_times_to_archive_and_recent_context(self):
        engine = worker.Engine.__new__(worker.Engine)
        engine.audio = [0]*45
        engine.speech = SimpleNamespace(preprocessor_config=SimpleNamespace(sample_rate=1))
        engine.transcript_log = TranscriptLog(self.directory/'aligned.txt')
        engine.mx = SimpleNamespace(get_active_memory=lambda:0)
        engine.recent = RecentTranscript(); engine.history = deque()
        engine.transcript_segments = [(102.,'Первая реплика.'),(132.,'Вторая реплика.')]
        engine.transcribe = lambda *args:'Первая реплика. Вторая реплика.'
        engine.process(self.wav,145.,summarize=False,keep_audio=True,request_id=7)
        self.assertEqual(engine.transcript_log.history.entries,engine.transcript_segments)
        self.assertEqual(engine.recent.entries,engine.transcript_segments)
        self.assertTrue(engine.transcript_log.was_committed(7,145.))

class CheckpointReplayTests(unittest.TestCase):
    def test_restart_does_not_redecode_committed_input_with_different_context(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'session.txt'
            stamp = time.time()
            original = TranscriptLog(path)
            original.update('Перенос встречи согласовали. Бюджет пока не утверждён.',stamp,45,request_id=17)
            before = path.read_bytes()
            restored = TranscriptLog(path,resume=True)
            engine = worker.Engine.__new__(worker.Engine)
            engine.transcript_log = restored
            engine.recent = RecentTranscript()
            engine.recent.entries = list(restored.history.entries)
            engine.mx = SimpleNamespace(get_active_memory=lambda: 0)
            engine.transcribe = lambda *args: self.fail('committed input must not run ASR again')
            result = engine.process(Path(directory)/'retry.wav',stamp,summarize=False,keep_audio=True,request_id=17)
            self.assertTrue(result['replayed'])
            self.assertEqual(path.read_bytes(),before)
            self.assertIn('Перенос встречи согласовали.',result['summary_context'])
            self.assertFalse(restored.was_committed(17,stamp+1))

if __name__ == '__main__':
    unittest.main()
