#!/usr/bin/env python3
"""Manual clean-room checks. The sole audio input is the pinned synthetic fixture.
Never enumerates Documents, a user's installed runtime, microphone or system audio.
"""
import argparse
import collections
import contextlib
import hashlib
import importlib.metadata
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE_SHA256 = 'ab59fe1aa9025ebe14c6fae09df53601b3f7ec747f358b103471482915d1125a'
TITLE_FIXTURE_TEXT = 'Обсудили сроки поставки оборудования. Поставку оборудования планируем на пятницу.'


def fixture_summary_semantics(source, summary, *, worker_validation_failed=False):
    """Finite subject/day regression for this pinned WAV, not a general fact checker.

    A response can be valid JSON and pass the worker's validator while changing
    the event. These independent fixture assertions deliberately do not reuse
    summary_writer's guards or tune any acceptance/holdout corpus.
    """
    def clauses(text):
        return [part for part in re.split(r'[.!?;,\n]+', text.lower().replace('ё', 'е')) if part.strip()]
    def meeting_friday(text):
        return any(re.search(r'\bвстреч\w*', part) and re.search(r'\bпятниц\w*', part)
                   for part in clauses(text))
    def release_friday_claim(text):
        return any(re.search(r'\bрелиз\w*', part) and re.search(r'\bпятниц\w*', part)
                   and not re.search(r'\bне\s+(?:назнач|перен|планир|состо|буд|выпус|выхо)\w*', part)
                   for part in clauses(text))
    checks = dict(source_has_expected_meeting_friday=meeting_friday(source),
                  model_summary_nonempty=bool(summary.strip()),
                  worker_validation_succeeded=not worker_validation_failed,
                  meeting_bound_to_friday=meeting_friday(summary),
                  release_not_assigned_to_friday=not release_friday_claim(summary))
    return dict(check_id='pinned-audio-meeting-friday-v1', fixture_sha256=FIXTURE_SHA256,
                scope='Finite subject/day checks only; other semantic errors and omissions are not excluded.',
                passed=all(checks.values()), checks=checks,
                failures=[name for name, passed in checks.items() if not passed])


def complete_acceptance_status(report):
    phases = [report.get(name, {}) for name in ('cold_inference', 'offline_second_inference')]
    report['semantic_fixture_passed'] = all(phase.get('fixture_summary_semantics', {}).get('passed', False)
                                             for phase in phases)
    report['passed'] = bool(report.get('runtime_transport_passed') and report['semantic_fixture_passed'])
    report['passed_scope'] = 'Runtime and named finite fixture checks; not general or human semantic acceptance.'
    report['general_semantic_acceptance'] = False


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(8*1024**2), b''):
            result.update(block)
    return result.hexdigest()


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')


def machine():
    memory = int(subprocess.check_output(['/usr/sbin/sysctl', '-n', 'hw.memsize'], text=True)) if sys.platform == 'darwin' else None
    return dict(architecture=platform.machine(), macos=platform.mac_ver()[0], physical_memory_bytes=memory,
                runner_label=os.environ.get('MEETING_CI_RUNNER'), github_run_id=os.environ.get('GITHUB_RUN_ID'),
                python=sys.version, virtual_machine_or_physical='not independently established')


def install(runtime):
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise RuntimeError('The manual MLX validation requires a native Apple Silicon macOS runner')
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError('Python3.12 is required')
    subprocess.run([sys.executable, '-m', 'venv', str(runtime)], check=True)
    python = runtime/'bin/python'
    with tempfile.TemporaryDirectory(prefix='meeting-ci-wheels-') as folder:
        wheels = []
        for entry in json.loads((PROJECT/'packaging/runtime-wheels.lock.json').read_text()):
            path = Path(folder)/entry['filename']
            with urllib.request.urlopen(entry['url'], timeout=60) as incoming, path.open('wb') as output:
                shutil.copyfileobj(incoming, output)
            if path.stat().st_size != entry['bytes'] or sha(path) != entry['sha256']:
                raise ValueError('Pinned runtime wheel digest mismatch')
            wheels.append(str(path))
        subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps', *wheels], check=True)
        subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--no-deps',
                        '--only-binary=:all:', '-r', str(PROJECT/'runtime/requirements.lock')], check=True)
        subprocess.run([str(python), '-m', 'pip', 'check'], check=True)


class Worker:
    def __init__(self, script, mode, session, transcript, fixture, label):
        self.events = queue.Queue()
        self.errors = collections.deque(maxlen=30)
        # Audit hook applies before imports and catches any unexpected network use.
        launcher = '''import sys,runpy
script=sys.argv[1]; arguments=sys.argv[2:]
sys.path.insert(0,str(__import__('pathlib').Path(script).parent))
def deny_network(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):
        raise RuntimeError('Network disabled during offline inference')
sys.addaudithook(deny_network)
sys.argv=[script]+arguments
runpy.run_path(script,run_name='__main__')
'''
        arguments = ['--mode', mode, '--session', str(session)]
        if mode == 'transcribe':
            arguments += ['--transcript-file', str(transcript), '--audio-file', str(fixture), '--audio-start', str(time.time()-17.281)]
        environment = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                           PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', TOKENIZERS_PARALLELISM='false',
                           OMP_NUM_THREADS='4', MEETING_SUMMARY_MODEL_LABEL=label)
        environment.pop('PYTHONPATH', None); environment.pop('PYTHONHOME', None)
        self.process = subprocess.Popen([sys.executable, '-B', '-u', '-c', launcher, str(script), *arguments],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1, env=environment)
        def output():
            for line in self.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except json.JSONDecodeError:
                    self.events.put(dict(event='error', text='Invalid worker JSON'))
            self.events.put(dict(event='exit', status=self.process.poll()))
        def errors():
            for line in self.process.stderr:
                self.errors.append(line[-1500:].rstrip())
        threading.Thread(target=output, daemon=True).start(); threading.Thread(target=errors, daemon=True).start()

    def wait(self, event, request_id=None, timeout=180):
        until = time.monotonic()+timeout
        while time.monotonic() < until:
            try:
                result = self.events.get(timeout=min(1, max(0.01, until-time.monotonic())))
            except queue.Empty:
                continue
            if result.get('event') in ('error', 'exit'):
                raise RuntimeError(f'Worker failed: {result}; stderr: {list(self.errors)}')
            if result.get('event') == event and (request_id is None or result.get('request_id') == request_id):
                return result
        raise TimeoutError(f'Worker {event} timeout; stderr: {list(self.errors)}')

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False)+'\n'); self.process.stdin.flush()

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait(timeout=3)


def inference(receipt, directory, fixture, label):
    directory.mkdir()
    chunk = directory/'chunk-00000001.wav'; shutil.copyfile(fixture, chunk)
    transcript = directory/'synthetic.txt'
    began = time.monotonic()
    asr = Worker(Path(receipt['worker']), 'transcribe', directory, transcript, fixture, label)
    try:
        asr.wait('ready')
        asr.send(dict(request_id=1, path=str(chunk), timestamp=time.time()))
        recognized = asr.wait('result', 1)
        context = recognized.get('summary_context', '').strip()
        if len(context.split()) < 4 or not transcript.is_file() or '[' not in transcript.read_text():
            raise AssertionError('Synthetic audio did not produce a saved timestamped transcript')
    finally:
        asr.close()
    asr_elapsed = time.monotonic()-began
    began = time.monotonic()
    summary = Worker(Path(receipt['worker']), 'summarize', directory, transcript, fixture, label)
    try:
        summary.wait('ready')
        summary.send(dict(request_id=2, text=context, timestamp=time.time()))
        condensed = summary.wait('result', 2)
        summary.send(dict(request_id=3, title_text=TITLE_FIXTURE_TEXT))
        title = summary.wait('result', 3).get('title')
        if title is not None and not isinstance(title, str):
            raise AssertionError('Title protocol returned an invalid type')
    finally:
        summary.close()
    return dict(asr_wall_seconds=round(asr_elapsed, 3), summary_title_wall_seconds=round(time.monotonic()-began, 3),
                synthetic_asr=context, synthetic_summary=condensed.get('summary', ''),
                summary_input_source='ASR of the pinned synthetic audio fixture',
                summary_validation_failed=bool(condensed.get('validation_failed')),
                fixture_summary_semantics=fixture_summary_semantics(
                    context, condensed.get('summary', ''), worker_validation_failed=bool(condensed.get('validation_failed'))),
                synthetic_title=title, synthetic_title_input=TITLE_FIXTURE_TEXT,
                synthetic_title_input_source='Separate fixed text; not the audio or ASR transcript',
                title_safe_fallback=not bool(title), runtime_transport_passed=True,
                asr_peak_active_memory_mb=recognized.get('memory_mb'),
                summary_peak_active_memory_mb=condensed.get('memory_mb'),
                workers='sequential ASR then summary; simultaneous app load is not validated',
                worker_network='all socket connect/getaddrinfo denied by Python audit hook')


def validate(profile, output):
    report = dict(format=2, profile=profile, passed=False, runtime_transport_passed=False,
                  semantic_fixture_passed=None, general_semantic_acceptance=False, fixture_sha256=FIXTURE_SHA256,
                  fixture_seconds=17.281, fixture_is_synthetic=True, audio_capture_or_playback=False,
                  artifacts_contain_audio_or_images=False, physical_8gb_acceptance=False, machine=machine())
    write_report(output, report)
    try:
        if sys.platform != 'darwin' or platform.machine() != 'arm64':
            raise RuntimeError('Native arm64 macOS is required; no Intel or emulation fallback')
        if profile == 'standard' and report['machine']['physical_memory_bytes'] < 16*1024**3:
            raise RuntimeError('9B profile requires a configured arm64 runner with at least16GiB; standard7GB M1 runner cannot validate this profile')
        import mlx.core as mx
        if not mx.metal.is_available():
            raise RuntimeError('Runner has no Metal GPU; use an approved physical/self-hosted Apple Silicon runner')
        report['metal_available'] = True
        report['packages'] = {d.metadata['Name']: d.version for d in importlib.metadata.distributions()}
        fixture = PROJECT/'tests/meeting-sample.wav'
        if sha(fixture) != FIXTURE_SHA256:
            raise ValueError('The only authorized synthetic fixture does not match its pinned digest')
        spec = importlib.util.spec_from_file_location('ci_asset_bootstrap', PROJECT/'packaging/bootstrap.py')
        bootstrap = importlib.util.module_from_spec(spec); spec.loader.exec_module(bootstrap)
        with tempfile.TemporaryDirectory(prefix='meeting-clean-models-') as folder:
            temp = Path(folder); resources = temp/'resources'; resources.mkdir()
            shutil.copy2(PROJECT/'packaging/models.lock.json', resources/'models.lock.json')
            shutil.copytree(PROJECT/'packaging/model-overrides', resources/'model-overrides')
            (resources/'workers').mkdir()
            hashes = {}
            for source in sorted((PROJECT/'src').glob('*.py')):
                shutil.copy2(source, resources/'workers'/source.name); hashes[source.name] = sha(source)
            worker_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:20]
            (resources/'bundle-manifest.json').write_text(json.dumps(dict(worker_id=worker_id, workers=hashes)))
            root = temp/'cold-root'
            if root.exists():
                raise AssertionError('Cold root already exists')
            if (resources/'models.installed.lock.json').exists():
                raise AssertionError('Clean-room validation must use public assets, never installed-host overrides')
            started = time.monotonic()
            # Stream download progress into job logs; no model/audio bytes are printed.
            receipt = bootstrap.prepare(resources, root, profile=profile)
            report['cold_setup_seconds'] = round(time.monotonic()-started, 3)
            report['model_manifest_sha256'] = receipt['model_manifest_sha256']; report['worker_hashes'] = hashes
            assets = Path(receipt['models'])
            def snapshot():
                return {str(p.relative_to(assets)): (p.stat().st_size, p.stat().st_mtime_ns, sha(p))
                        for p in sorted(assets.rglob('*')) if p.is_file()}
            before = snapshot()
            label = 'Qwen3.5 4B' if profile == 'light' else 'Qwen3.5 9B'
            report['cold_inference'] = inference(receipt, temp/'first-inference', fixture, label)
            original_urlopen = bootstrap.urllib.request.urlopen
            def no_network(*args, **kwargs):
                raise AssertionError('Second startup attempted a network request')
            bootstrap.urllib.request.urlopen = no_network
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    second = bootstrap.prepare(resources, root, offline=True, profile=profile)
            finally:
                bootstrap.urllib.request.urlopen = original_urlopen
            if before != snapshot() or second['worker'] != receipt['worker']:
                raise AssertionError('Second startup changed model bytes/mtime or worker path')
            report['second_start_assets_unchanged'] = True
            report['offline_second_inference'] = inference(second, temp/'second-inference', fixture, label)
            if before != snapshot():
                raise AssertionError('Offline inference modified model assets')
            report['runtime_transport_passed'] = True
            complete_acceptance_status(report)
            if not report['semantic_fixture_passed']:
                raise AssertionError('Runtime passed but the pinned-fixture semantic checks failed; inspect per-phase failures')
    except BaseException as error:
        report['error'] = type(error).__name__+': '+str(error)
        raise
    finally:
        write_report(output, report)


def self_test():
    with tempfile.TemporaryDirectory(prefix='meeting-ci-protocol-fixture-') as folder:
        directory = Path(folder)
        script = directory/'fixture_worker.py'
        script.write_text("""import json,socket,sys
print(json.dumps({'event':'ready'}),flush=True)
for line in sys.stdin:
    request=json.loads(line)
    denied=False
    try:socket.create_connection(('127.0.0.1',9),timeout=0.1)
    except RuntimeError as error:denied='Network disabled' in str(error)
    print(json.dumps({'event':'result','request_id':request['request_id'],'network_denied':denied}),flush=True)
""")
        worker = Worker(script, 'summarize', directory, directory/'unused.txt', directory/'unused.wav', 'fixture')
        try:
            worker.wait('ready', timeout=5)
            worker.send(dict(request_id=17))
            result = worker.wait('result', 17, timeout=5)
            if not result['network_denied']:
                raise AssertionError('Offline network audit was not installed before worker imports')
        finally:
            worker.close()
        if worker.process.poll() is None:
            raise AssertionError('Fixture worker was not reaped')
    source = ('Коллеги, встречу переносим с четверга на пятницу. Анна подготовит смету к вечеру. '
              'Бюджет остается 100 тысяч рублей. Сегодня релиз не выпускаем, потому что тесты '
              'авторизации еще не прошли. Сначала исправим ошибки. Новую дату релиза пока не назначили.')
    correct = ('Встречу переносим с четверга на пятницу. Анна подготовит смету к вечеру. '
               'Бюджет остается 100 тысяч рублей. Сегодня релиз не выпускаем: тесты авторизации '
               'еще не прошли. Сначала исправим ошибки. Новую дату релиза пока не назначили.')
    assert fixture_summary_semantics(source, correct)['passed']
    for subject in ('Метод', 'Релиз', 'Бюджет'):
        wrong = correct.replace('Встречу переносим', subject+' перенесен')
        checks = fixture_summary_semantics(source, wrong)
        assert not checks['passed'], 'Wrong Friday-event subject was accepted: '+subject
        assert 'meeting_bound_to_friday' in checks['failures']
    wrong_release = correct+' Релиз назначен на пятницу.'
    assert not fixture_summary_semantics(source, wrong_release)['passed']
    compressed = 'Встреча состоится в пятницу, а релиз пока не выпускаем.'
    assert fixture_summary_semantics(source, compressed)['passed']
    broken = dict(runtime_transport_passed=True, cold_inference={
        'fixture_summary_semantics': fixture_summary_semantics(source, 'Метод перенесен на пятницу.')},
        offline_second_inference={'fixture_summary_semantics': fixture_summary_semantics(source, correct)})
    complete_acceptance_status(broken)
    assert broken['runtime_transport_passed'] and not broken['semantic_fixture_passed'] and not broken['passed']
    assert TITLE_FIXTURE_TEXT and TITLE_FIXTURE_TEXT != source
    # Exercise the real report constructor with synthetic protocol responses:
    # a valid separate equipment title must not bless a corrupted audio summary.
    from unittest.mock import patch
    for returned_title in ('Обсуждение сроков поставки оборудования', None):
        sent = []
        class FakeWorker:
            def __init__(self, script, mode, session, transcript, fixture, label):
                self.mode = mode
                if mode == 'transcribe':
                    transcript.write_text('[00:00:00] synthetic fixture row')
            def wait(self, event, request_id=None, timeout=180):
                if event == 'ready':
                    return dict(event='ready')
                if self.mode == 'transcribe':
                    return dict(summary_context=source, memory_mb=646)
                if request_id == 2:
                    return dict(summary='Метод перенесен на пятницу.', validation_failed=False, memory_mb=2258)
                return dict(title=returned_title)
            def send(self, request):
                sent.append(request)
            def close(self):
                pass
        with tempfile.TemporaryDirectory(prefix='meeting-ci-report-check-') as folder:
            base = Path(folder); fixture = base/'fake.wav'; fixture.write_bytes(b'not decoded or played')
            with patch.dict(globals(), Worker=FakeWorker):
                result = inference({'worker': str(base/'unused.py')}, base/'run', fixture, 'fixture')
        assert result['runtime_transport_passed'] and not result['fixture_summary_semantics']['passed']
        assert result['synthetic_title_input'] == TITLE_FIXTURE_TEXT != result['synthetic_asr']
        assert result['synthetic_title_input_source'].startswith('Separate fixed text')
        assert result['title_safe_fallback'] == (returned_title is None)
        assert sent[-1]['title_text'] == TITLE_FIXTURE_TEXT
    print('PASS: real child protocol/network denial/cleanup; independent fixture event-binding and runtime-versus-semantic status checks; no model/audio operations')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    setup = sub.add_parser('install'); setup.add_argument('--runtime', type=Path, required=True)
    check = sub.add_parser('validate'); check.add_argument('--profile', choices=['light', 'standard'], required=True)
    check.add_argument('--report', type=Path, required=True)
    sub.add_parser('self-test')
    args = parser.parse_args()
    if args.command == 'self-test':
        self_test()
    elif args.command == 'install':
        install(args.runtime.resolve())
    else:
        validate(args.profile, args.report.resolve())


if __name__ == '__main__':
    main()
