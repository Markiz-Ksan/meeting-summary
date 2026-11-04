import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline_eval', ROOT / 'scripts/evaluate_summaries.py')
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
FILES = ('worker.py', 'summary_writer.py', 'recent_transcript.py', 'transcript_log.py', 'session_title.py')


class PipelineFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'src'
        self.source.mkdir()
        for name in FILES:
            (self.source / name).write_text('# fake pipeline ' + name)
        self.model = self.root / 'runtime/models/summary-9b'
        self.model.mkdir(parents=True)
        (self.model / 'config.json').write_text('{}')
        (self.model / 'model.safetensors').write_bytes(b'fake weights, never loaded')
        self.corpus = self.root / 'corpus'
        self.corpus.mkdir()
        self.case = dict(id='FAKE', source='Artificial', expect_empty=False,
                         critical_facts=['Artificial'], important_facts=[], forbidden_inferences=['Wrong'])
        for name in ('development.json', 'holdout.json'):
            (self.corpus / name).write_text(json.dumps([self.case]))
        (self.corpus / 'manifest.json').write_text(json.dumps({'files': {
            name: evaluation.sha(self.corpus / name) for name in ('development.json', 'holdout.json')}}))
        self.freeze = self.root / 'freeze.json'
        self.frozen = dict(
            pipeline_method='worker-cli-summarize-v1', environment_identity={'python': 'fake'},
            writer_sha256=evaluation.sha(self.source / 'summary_writer.py'),
            pipeline_sha256={n: evaluation.sha(self.source / n) for n in FILES},
            model_files_sha256={p.name: evaluation.sha(p) for p in self.model.iterdir()},
            corpus_manifest_sha256=evaluation.sha(self.corpus / 'manifest.json'),
            development_review_status='passed')
        self.freeze.write_text(json.dumps(self.frozen))
        self.addCleanup(patch.stopall)
        patch.object(evaluation, 'ROOT', self.root).start()
        patch.object(evaluation, 'CORPUS', self.corpus).start()
        patch.object(evaluation, 'environment_identity', return_value={'python': 'fake'}, create=True).start()

    def test_worker_change_cannot_reuse_unchanged_writer_freeze(self):
        (self.source / 'worker.py').write_text('# preprocessing changed')
        with self.assertRaisesRegex(ValueError, 'Pipeline'):
            evaluation.load_cases('holdout', self.freeze)

    def test_weight_change_cannot_reuse_unchanged_configuration(self):
        (self.model / 'model.safetensors').write_bytes(b'different fake weights')
        with self.assertRaisesRegex(ValueError, 'Model'):
            evaluation.load_cases('holdout', self.freeze)

    def test_legacy_writer_only_freeze_is_rejected(self):
        self.frozen.pop('pipeline_sha256')
        self.freeze.write_text(json.dumps(self.frozen))
        with self.assertRaisesRegex(ValueError, 'Pipeline'):
            evaluation.load_cases('holdout', self.freeze)

    def test_matching_freeze_reads_only_the_fake_holdout(self):
        self.assertEqual(evaluation.load_cases('holdout', self.freeze),
                         [self.case])

    def test_writer_only_evaluation_method_cannot_authorize_pipeline(self):
        self.frozen['pipeline_method'] = 'writer-only'
        self.freeze.write_text(json.dumps(self.frozen))
        with self.assertRaisesRegex(ValueError, 'Pipeline method'):
            evaluation.load_cases('holdout', self.freeze)

    def test_changed_dependency_environment_invalidates_freeze(self):
        self.frozen['environment_identity'] = {'python': 'old'}
        self.freeze.write_text(json.dumps(self.frozen))
        with self.assertRaisesRegex(ValueError, 'Environment'):
            evaluation.load_cases('holdout', self.freeze)

    def test_change_between_freeze_check_and_snapshot_cannot_start_worker(self):
        load_cases = evaluation.load_cases
        def load_then_change(*args):
            cases = load_cases(*args)
            (self.source / 'worker.py').write_text('# changed after freeze validation')
            return cases
        argv = ['evaluate', '--split', 'holdout', '--freeze', str(self.freeze),
                '--output', str(self.root / 'runs'), '--source-dir', str(self.source)]
        with patch.object(sys, 'argv', argv), patch.object(evaluation, 'load_cases', side_effect=load_then_change), \
                patch.object(evaluation, 'SummaryWorker') as worker, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'Frozen runtime'):
                evaluation.main()
            worker.assert_not_called()

    def test_source_changed_during_run_is_not_reported_as_complete(self):
        source = self.source
        model = self.model.resolve()
        class FakeWorker:
            def __init__(self, script, session, stderr):
                assert script.parent != source
                assert (script.parent.parent / 'runtime/models/summary-9b').resolve() == model
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass
            def summarize(self, text, request_id):
                (source / 'worker.py').write_text('# changed while evaluation was running')
                return {'summary': '• Artificial'}
        output = self.root / 'runs'
        argv = ['evaluate', '--output', str(output), '--source-dir', str(source)]
        with patch.object(sys, 'argv', argv), patch.object(evaluation, 'SummaryWorker', FakeWorker), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'changed during evaluation'):
                evaluation.main()
        metadata = json.loads(next(output.glob('*/metadata.json')).read_text())
        self.assertFalse(metadata['completed'])
        self.assertFalse(metadata['live_source_unchanged'])


class EvaluationResultTests(unittest.TestCase):
    def test_expected_empty_is_valid_only_without_rejection(self):
        case = {'id': 'SOCIAL', 'source': 'Hello', 'expect_empty': True}
        result = evaluation.result_row(case, {'summary': ''}, 0.1)
        self.assertTrue(result['structure_pass'])
        rejected = evaluation.result_row(case, {'summary': '', 'validation_failed': True}, 0.1)
        self.assertFalse(rejected['structure_pass'])
        self.assertEqual(rejected['error'], 'draft_rejected')

    def test_empty_substantive_source_is_not_counted_as_success(self):
        case = {'id': 'FACT', 'source': 'Meeting Friday', 'expect_empty': False}
        self.assertFalse(evaluation.result_row(case, {'summary': ''}, 0.1)['structure_pass'])


class WorkerEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.script = self.folder / 'worker.py'
        self.script.write_text('''import argparse,json,sys
p=argparse.ArgumentParser();p.add_argument('--mode');p.add_argument('--session');a=p.parse_args()
assert a.mode=='summarize', 'Evaluator must never select audio mode'
print(json.dumps({'event':'loading'}),flush=True)
print(json.dumps({'event':'ready','stage':'summarize'}),flush=True)
for line in sys.stdin:
 r=json.loads(line)
 print(json.dumps({'event':'progress','request_id':r['request_id']}),flush=True)
 if r['text']=='timeout':
  import time;time.sleep(20)
 print(json.dumps({'event':'result','request_id':r['request_id'],'summary':'• '+r['text']}),flush=True)
''')

    def test_protocol_pipeline_runs_twice_in_one_process_and_closes(self):
        with evaluation.SummaryWorker(self.script, self.folder, self.folder / 'stderr.log') as worker:
            first = worker.summarize('First fact', 1, timeout=3)
            self.assertEqual(first['summary'], '• First fact')
            second = worker.summarize('Second fact', 2, timeout=3)
            self.assertEqual(second['summary'], '• Second fact')
            process = worker.process
        self.assertIsNotNone(process.poll())

    def test_request_timeout_leaves_no_child_process(self):
        with self.assertRaises(TimeoutError):
            with evaluation.SummaryWorker(self.script, self.folder, self.folder / 'stderr.log') as worker:
                process = worker.process
                worker.summarize('timeout', 1, timeout=0.02)
        self.assertIsNotNone(process.poll())

    def test_startup_failure_reaps_child_even_when_constructor_raises(self):
        self.script.write_text("import json,time\nprint(json.dumps({'event':'error'}),flush=True)\ntime.sleep(20)\n")
        real_popen = subprocess.Popen
        children = []
        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with patch.object(evaluation.subprocess, 'Popen', side_effect=popen):
            with self.assertRaisesRegex(RuntimeError, 'startup'):
                evaluation.SummaryWorker(self.script, self.folder, self.folder / 'stderr.log')
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())


if __name__ == '__main__':
    unittest.main()
