"""Small offline text evaluation. It never opens audio devices or grades semantics.

Development is the default. Holdout requires a frozen code/model manifest.
Every run is immutable and includes a review worksheet; output != acceptance.
"""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import select
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'tests/fixtures/acceptance-v1'
PIPELINE_FILES = ('worker.py', 'summary_writer.py', 'recent_transcript.py', 'transcript_log.py', 'session_title.py')
PIPELINE_METHOD = 'worker-cli-summarize-v1'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def pipeline_hashes(source_dir):
    return {name: sha(source_dir / name) for name in PIPELINE_FILES}


def environment_identity():
    versions = {}
    for name in ('mlx', 'mlx-lm', 'tokenizers', 'transformers', 'safetensors'):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return dict(python=sys.version, architecture=platform.machine(), macos=platform.mac_ver()[0],
                packages=versions)


def model_hashes(source_dir):
    model = source_dir.parent / 'runtime/models/summary-9b'
    files = sorted(p for p in model.iterdir() if p.is_file() and p.suffix in {'.json', '.safetensors', '.jinja'})
    if not (model / 'config.json').is_file() or not any(p.suffix == '.safetensors' for p in files):
        raise ValueError('Model configuration or weights missing')
    return {p.name: sha(p) for p in files}


class SummaryWorker:
    """The real summary-only CLI, with bounded protocol reads and cleanup."""
    def __init__(self, script, session, stderr_path):
        self.buffer = bytearray()
        self.stderr = stderr_path.open('wb')
        try:
            self.process = subprocess.Popen(
                [sys.executable, '-B', str(script), '--mode', 'summarize', '--session', str(session)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr)
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            deadline = time.monotonic() + 120
            while True:
                event = self.read_event(deadline)
                if event.get('event') == 'ready':
                    break
                if event.get('event') in ('error', 'protocol_error'):
                    raise RuntimeError('Worker startup failed')
        except BaseException:
            self.close()
            raise

    def read_event(self, deadline):
        while True:
            if b'\n' in self.buffer:
                line, _, tail = self.buffer.partition(b'\n')
                self.buffer = bytearray(tail)
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('Worker protocol event is not an object')
                return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Summary worker response timeout')
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise TimeoutError('Summary worker response timeout')
            try:
                data = os.read(self.process.stdout.fileno(), 65536)
            except BlockingIOError:
                continue
            if not data:
                raise RuntimeError('Summary worker ended before its response')
            self.buffer.extend(data)
            if len(self.buffer) > 1024 * 1024:
                raise ValueError('Summary worker protocol limit exceeded')

    def summarize(self, text, request_id, timeout=90):
        deadline = time.monotonic() + timeout
        payload = memoryview((json.dumps(dict(text=text, timestamp=time.time(), request_id=request_id),
                                        ensure_ascii=False) + '\n').encode())
        while payload:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], max(0, remaining))[1]:
                raise TimeoutError('Summary worker input timeout')
            try:
                count = os.write(self.process.stdin.fileno(), payload)
            except BlockingIOError:
                continue
            if not count:
                raise BrokenPipeError('Summary worker input closed')
            payload = payload[count:]
        while True:
            event = self.read_event(deadline)
            if event.get('event') in ('error', 'protocol_error'):
                raise RuntimeError('Summary worker protocol failure')
            if event.get('event') == 'result':
                if type(event.get('request_id')) is not int or event['request_id'] != request_id:
                    raise ValueError('Summary worker result does not match request')
                return event

    def close(self):
        process = getattr(self, 'process', None)
        try:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                for pipe in (process.stdin, process.stdout):
                    if pipe is not None:
                        pipe.close()
        finally:
            self.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def result_row(case, result, seconds):
    text = result.get('summary')
    if not isinstance(text, str):
        raise ValueError('Summary result contains no text field')
    rejected = result.get('validation_failed') is True
    points = [p[2:] for p in text.splitlines() if p.startswith('• ')]
    structure = bool(points) and 1 <= len(points) <= 4 and all(len(p.split()) <= 40 for p in points) and sum(len(p.split()) for p in points) <= 80 and len(points) == len(text.splitlines())
    if case['expect_empty']:
        structure = not text.strip()
    return dict(id=case['id'], summary=text, error='draft_rejected' if rejected else None,
                validation_failed=rejected, seconds=round(seconds, 3), worker_seconds=result.get('seconds'),
                structure_pass=structure and not rejected, expected_empty=case['expect_empty'])


def load_cases(split, freeze=None, source_dir=None):
    source_dir = source_dir or ROOT / 'src'
    manifest = json.loads((CORPUS / 'manifest.json').read_text())
    for name, expected in manifest['files'].items():
        if sha(CORPUS / name) != expected:
            raise ValueError('Corpus changed after annotation: ' + name)
    if split == 'holdout':
        if freeze is None:
            raise ValueError('Holdout requires --freeze pointing to a frozen-pipeline manifest')
        frozen = json.loads(freeze.read_text())
        if frozen.get('writer_sha256') != sha(source_dir / 'summary_writer.py'):
            raise ValueError('Writer does not match the frozen version')
        if frozen.get('pipeline_sha256') != pipeline_hashes(source_dir):
            raise ValueError('Pipeline does not match the frozen version')
        if frozen.get('pipeline_method') != PIPELINE_METHOD:
            raise ValueError('Pipeline method was not frozen')
        if frozen.get('environment_identity') != environment_identity():
            raise ValueError('Environment does not match the frozen version')
        if frozen.get('corpus_manifest_sha256') != sha(CORPUS / 'manifest.json'):
            raise ValueError('Corpus does not match the frozen version')
        if frozen.get('development_review_status') != 'passed':
            raise ValueError('Development review has not passed')
        if frozen.get('model_files_sha256') != model_hashes(source_dir):
            raise ValueError('Model does not match the frozen version')
    return json.loads((CORPUS / (split + '.json')).read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--split', choices=['development', 'holdout'], default='development')
    parser.add_argument('--freeze', type=Path)
    parser.add_argument('--source-dir', type=Path, default=ROOT / 'src',
                        help='Worker source directory; select installed src to evaluate the deployed version')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    if args.limit is not None and (args.limit <= 0 or args.split == 'holdout'):
        parser.error('--limit must be positive and is development-only')
    source = args.source_dir.resolve(strict=True)
    # Check holdout gates before process creation, MLX imports or model loading.
    cases = load_cases(args.split, args.freeze, source)
    if args.limit is not None:
        cases = cases[:args.limit]
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    run = args.output / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    run.mkdir(parents=True, exist_ok=False)
    identity = pipeline_hashes(source)
    model = source.parent / 'runtime/models/summary-9b'
    model_stats = lambda: {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                           for p in model.iterdir() if p.is_file() and p.suffix in {'.json', '.jinja', '.safetensors'}}
    before_model = model_stats()
    model_identity = model_hashes(source)
    if before_model != model_stats():
        raise RuntimeError('Model changed during fingerprinting')
    snapshot = run / 'src'
    snapshot.mkdir()
    for name in PIPELINE_FILES:
        shutil.copyfile(source / name, snapshot / name)
    if pipeline_hashes(snapshot) != identity or pipeline_hashes(source) != identity:
        raise RuntimeError('Pipeline changed during snapshot creation')
    links = run / 'runtime/models'
    links.mkdir(parents=True)
    (links / 'summary-9b').symlink_to(model.resolve(strict=True), target_is_directory=True)
    session = run / 'session'
    session.mkdir()
    metadata = dict(split=args.split, pipeline_method=PIPELINE_METHOD,
                    source_dir=str(source), pipeline_sha256=identity,
                    writer_sha256=identity['summary_writer.py'], model_files_sha256=model_identity,
                    corpus_manifest_sha256=sha(CORPUS / 'manifest.json'),
                    environment_identity=environment_identity(),
                    python_version=sys.version.split()[0], model='existing Qwen3.5-9B MLX 4bit',
                    reasoning=False, audio_capture=False, audio_playback=False,
                    semantic_acceptance='unreviewed', completed=False)
    if args.split == 'holdout':
        # Validate the captured inputs too: they may have changed since load_cases.
        frozen = json.loads(args.freeze.read_text())
        fields = ('pipeline_method', 'writer_sha256', 'pipeline_sha256',
                  'model_files_sha256', 'environment_identity', 'corpus_manifest_sha256')
        if frozen.get('development_review_status') != 'passed' or any(
                frozen.get(key) != metadata[key] for key in fields):
            raise ValueError('Frozen runtime does not match evaluation snapshot')
    metadata_path = run / 'metadata.json'
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
    worksheet = []
    try:
        with SummaryWorker(snapshot / 'worker.py', session, run / 'worker-stderr.log') as worker:
            for index, case in enumerate(cases, 1):
                started = time.monotonic()
                result = worker.summarize(case['source'], index)
                row = result_row(case, result, time.monotonic() - started)
                review = dict(case, output=row,
                              critical_review=[dict(fact=f, preserved=None) for f in case['critical_facts']],
                              important_review=[dict(fact=f, preserved=None) for f in case['important_facts']],
                              forbidden_review=[dict(inference=f, present=None) for f in case['forbidden_inferences']],
                              semantic_duplicates=None, unsupported_claims=[], reviewer=None, status='unreviewed')
                worksheet.append(review)
                with (run / 'results.jsonl').open('a') as stream:
                    stream.write(json.dumps(row, ensure_ascii=False) + '\n')
                (run / 'review.json').write_text(json.dumps(worksheet, ensure_ascii=False, indent=2) + '\n')
                print(json.dumps(row, ensure_ascii=False), flush=True)
        metadata.update(completed=True)
    except BaseException as error:
        metadata['run_error'] = type(error).__name__ + ': ' + str(error)
        raise
    finally:
        try:
            live_source_unchanged = pipeline_hashes(source) == identity
        except OSError:
            live_source_unchanged = False
        try:
            model_files_unchanged = model_stats() == before_model
        except OSError:
            model_files_unchanged = False
        metadata.update(cases=len(worksheet), live_source_unchanged=live_source_unchanged,
                        model_files_unchanged=model_files_unchanged,
                        environment_unchanged=environment_identity() == metadata['environment_identity'])
        if not all(metadata[key] for key in ('live_source_unchanged', 'model_files_unchanged', 'environment_unchanged')):
            metadata['completed'] = False
            metadata.setdefault('run_error', 'Pipeline, model or environment changed during evaluation')
        metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
        print(str(run), flush=True)
    if not metadata['completed']:
        raise RuntimeError(metadata['run_error'])


if __name__ == '__main__':
    main()
