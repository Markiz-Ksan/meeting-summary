#!/usr/bin/env python3
"""Build a relocatable Apple Silicon app. The output is a ZIP, never a project .app."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
CANONICAL = Path.home()/'Library/Application Support/MeetingOverlay'


def run(*args, **kwargs):
    try:
        return subprocess.run([str(x) for x in args], check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or '')[-6000:]
        raise RuntimeError(str(error)+'\n'+str(detail)) from error


def sha(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024**2), b''):
            value.update(block)
    return value.hexdigest()


def macho(path):
    if path.is_symlink() or not path.is_file():
        return False
    with path.open('rb') as stream:
        return stream.read(4) in [b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca']


def relocate(runtime):
    paths = [path for path in runtime.rglob('*') if macho(path)]
    for path in paths:
        identifiers = subprocess.check_output(['otool', '-arch', 'arm64', '-D', str(path)], text=True).splitlines()[1:]
        identifiers = [value.strip() for value in identifiers if value.strip()]
        if identifiers:
            run('install_name_tool', '-id', '@rpath/'+path.name, path,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        libraries = subprocess.check_output(['otool', '-arch', 'arm64', '-L', str(path)], text=True).splitlines()[1:]
        for line in libraries:
            dependency = line.strip().split(' (')[0]
            if dependency.startswith(('@', '/System/', '/usr/lib/')):
                continue
            candidates = [candidate for candidate in paths if candidate.name == Path(dependency).name]
            if len(candidates) != 1:
                raise RuntimeError('Cannot resolve native library: '+dependency)
            local = '@loader_path/'+os.path.relpath(candidates[0], path.parent)
            run('install_name_tool', '-change', dependency, local, path,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def audit(bundle):
    files = []
    for path in bundle.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(bundle.resolve()):
            raise RuntimeError('External symlink: '+str(path))
        if not macho(path):
            continue
        arch = subprocess.check_output(['lipo', '-archs', str(path)], text=True).strip()
        if 'arm64' not in arch.split():
            raise RuntimeError('No arm64 architecture: '+str(path))
        libraries = subprocess.check_output(['otool', '-arch', 'arm64', '-L', str(path)], text=True).splitlines()[1:]
        for line in libraries:
            dependency = line.strip().split(' (')[0]
            if not dependency.startswith(('@', '/System/', '/usr/lib/')):
                raise RuntimeError('External native library: '+dependency+' in '+str(path))
        # Embedded dependencies must not raise the minimum OS beyond the UI's floor.
        load = subprocess.check_output(['otool', '-arch', 'arm64', '-l', str(path)], text=True).splitlines()
        minimum = []
        for index, line in enumerate(load):
            if 'cmd LC_BUILD_VERSION' in line:
                for following in load[index+1:index+7]:
                    if following.strip().startswith('minos '):
                        version = following.split()[-1]
                        minimum.append(version)
                        pieces = tuple(int(v) for v in version.split('.')[:2])
                        if pieces > (14, 4):
                            raise RuntimeError('Dependency needs macOS '+version+': '+str(path))
        files.append(dict(path=str(path.relative_to(bundle)), architectures=arch, minimum_macos=minimum))
    return files


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--runtime-source', type=Path, default=CANONICAL/'runtime')
    p.add_argument('--worker-source', type=Path, default=CANONICAL/'src')
    p.add_argument('--output', type=Path, default=PROJECT/'.build.noindex/meeting-overlay-arm64-candidate.zip')
    p.add_argument('--version', default='0.7.0')
    p.add_argument('--build', default='16')
    p.add_argument('--sign', default='-', help='Developer ID Application identity for distribution; - for local candidate')
    p.add_argument('--models', type=Path, help='Optional exact verified model files for fully offline package')
    p.add_argument('--notary-profile', help='Explicit existing notarytool keychain profile; requires Developer ID signing')
    args = p.parse_args()
    if os.uname().machine != 'arm64':
        p.error('Run packaging natively on Apple Silicon')
    runtime = args.runtime_source.resolve()
    workers = args.worker_source.resolve()
    if not (workers/'worker.py').is_file():
        p.error('Reviewed worker source missing')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='meeting-portable-', dir='/private/tmp') as temporary:
        temp = Path(temporary)
        bundle = temp/'Сводка встречи.app'
        resources = bundle/'Contents/Resources'
        binary = bundle/'Contents/MacOS/MeetingOverlay'
        resources.mkdir(parents=True); binary.parent.mkdir(parents=True)
        swift_source = temp/'swift-source'; swift_source.mkdir()
        for source in (PROJECT/'src').glob('*.swift'):
            shutil.copy2(source, swift_source/source.name)
        swift = sorted(swift_source.glob('*.swift'))
        run('xcrun', 'swiftc', '-swift-version', '5', '-O', '-target', 'arm64-apple-macosx14.4',
            '-module-cache-path', temp/'swift-cache', '-framework', 'AppKit', '-framework', 'AVFoundation',
            '-framework', 'CoreAudio', *swift, '-o', binary)
        info = dict(CFBundleExecutable='MeetingOverlay', CFBundleIdentifier='local.ksandr.meeting-overlay',
                    CFBundleName='Сводка встречи', CFBundleDisplayName='Сводка встречи', CFBundlePackageType='APPL',
                    CFBundleShortVersionString=args.version, CFBundleVersion=args.build, CFBundleIconFile='MeetingSummary.icns',
                    LSUIElement=True, LSMinimumSystemVersion='14.4', LSArchitecturePriority=['arm64'],
                    NSMicrophoneUsageDescription='Выбранный микрофон обрабатывается локально. Запись начинается только по вашей команде.',
                    NSAudioCaptureUsageDescription='Звук приложений обрабатывается локально для сводки встречи. Изображение экрана не захватывается.')
        with (bundle/'Contents/Info.plist').open('wb') as output:
            plistlib.dump(info, output)
        shutil.copy2(PROJECT/'assets/MeetingOverlay.icns', resources/'MeetingSummary.icns')
        shutil.copy2(PROJECT/'tests/meeting-sample.wav', resources/'meeting-sample.wav')
        shutil.copy2(PROJECT/'packaging/bootstrap.py', resources/'bootstrap.py')
        selected_manifest = 'models.installed.lock.json' if args.models else 'models.lock.json'
        shutil.copy2(PROJECT/'packaging'/selected_manifest, resources/'models.lock.json')
        shutil.copy2(PROJECT/'packaging/models.installed.lock.json', resources/'models.installed.lock.json')
        shutil.copytree(PROJECT/'packaging/model-overrides', resources/'model-overrides')
        shutil.copytree(PROJECT/'packaging/licenses', resources/'licenses')
        (resources/'workers').mkdir()
        worker_hashes = {}
        for source in sorted(workers.glob('*.py')):
            if source.name.startswith('test_'):
                continue
            shutil.copy2(source, resources/'workers'/source.name)
            worker_hashes[source.name] = sha(source)
        worker_id = hashlib.sha256(json.dumps(worker_hashes, sort_keys=True).encode()).hexdigest()[:20]
        (resources/'bundle-manifest.json').write_text(json.dumps(dict(format=1, worker_id=worker_id,
             workers=worker_hashes, python='3.12.13', architecture='arm64', minimum_memory_gb=8,
             recommended_memory_gb=24, models_bundled=bool(args.models)), ensure_ascii=False, indent=2)+'\n')
        python = resources/'runtime/python'
        shutil.copytree(runtime/'python', python, symlinks=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        site = python/'lib/python3.12/site-packages'
        shutil.copytree(runtime/'.venv/lib/python3.12/site-packages', site, dirs_exist_ok=True, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        # The author's MLX wheel targets macOS26.2. Use the same version's official14.0 wheels.
        wheel_lock = json.loads((PROJECT/'packaging/runtime-wheels.lock.json').read_text())
        for name in ('mlx', 'mlx-0.32.2.dist-info', 'mlx_metal-0.32.2.dist-info'):
            existing = site/name
            if existing.exists():
                shutil.rmtree(existing)
        for entry in wheel_lock:
            wheel = PROJECT/'packaging/runtime-wheels'/entry['filename']
            if not wheel.is_file():
                wheel = temp/entry['filename']
                run('/usr/bin/curl', '--fail', '--location', '--retry', '3', '--proto', '=https', '--output', wheel, entry['url'])
            if wheel.stat().st_size != entry['bytes'] or sha(wheel) != entry['sha256']:
                raise RuntimeError('Damaged pinned runtime wheel: '+str(wheel))
            with zipfile.ZipFile(wheel) as archive:
                if any(Path(name).is_absolute() or '..' in Path(name).parts for name in archive.namelist()):
                    raise RuntimeError('Unsafe wheel path')
                archive.extractall(site)
        # Only the interpreter is used. Installed console-script shebangs refer to the author's venv.
        for path in (python/'bin').iterdir():
            if path.name not in ('python3.12', 'python', 'python3'):
                path.unlink() if not path.is_dir() else shutil.rmtree(path)
        for name in ('python', 'python3'):
            path = python/'bin'/name
            path.unlink(missing_ok=True); path.symlink_to('python3.12')
        # Avoid user-site or pip launchers from influencing the embedded interpreter.
        for pth in site.glob('*.pth'):
            if any(line.strip() and not line.startswith(('#', 'import ')) and Path(line.strip()).is_absolute()
                   for line in pth.read_text().splitlines()):
                raise RuntimeError('Absolute site-packages path: '+str(pth))
        if args.models:
            manifest = json.loads((resources/'models.lock.json').read_text())
            for model in manifest['models']:
                for entry in model['files']:
                    source = args.models/model['directory']/entry['name']
                    if not source.is_file() or source.stat().st_size != entry['size'] or sha(source) != entry['sha256']:
                        raise RuntimeError('Offline model differs from manifest: '+str(source))
                    target = resources/'models'/model['directory']/entry['name']
                    target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
        relocate(python)
        native = audit(bundle)
        # Sign every embedded Mach-O first. Resource binaries are covered by the outer signature.
        paths = [bundle/item['path'] for item in native]
        for path in sorted(paths, key=lambda x: len(x.parts), reverse=True):
            command = ['codesign', '--force', '--sign', args.sign]
            if args.sign != '-':
                command += ['--options', 'runtime', '--timestamp', '--entitlements', PROJECT/'packaging/runtime.entitlements']
            run(*command, path, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        command = ['codesign', '--force', '--sign', args.sign, '--identifier', 'local.ksandr.meeting-overlay']
        if args.sign != '-':
            command += ['--options', 'runtime', '--timestamp', '--entitlements', PROJECT/'packaging/runtime.entitlements']
        run(*command, bundle)
        run('codesign', '--verify', '--deep', '--strict', bundle)
        # Isolated interpreter: fresh HOME, PATH without Python, no host PYTHONPATH or cached models.
        clean_home = temp/'clean-home'; clean_home.mkdir()
        environment = dict(HOME=str(clean_home), PATH='/usr/bin:/bin', TMPDIR=str(temp),
                           PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                           HF_HUB_DISABLE_TELEMETRY='1', NUMBA_CACHE_DIR=str(temp/'numba-cache'))
        result = run(python/'bin/python3.12', '-I', '-B', resources/'bootstrap.py', '--self-test',
                     env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        smoke = json.loads(result.stdout.strip().splitlines()[-1])
        staged = run(python/'bin/python3.12', '-I', '-B', resources/'bootstrap.py', '--root', clean_home/'isolated',
                     '--offline', '--skip-models', env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        setup = json.loads(staged.stdout.strip().splitlines()[-1])
        # Validate the actual staged worker can import only bundled dependencies and parse its protocol arguments.
        parsed = run(python/'bin/python3.12', '-B', setup['worker'], '--help', env=environment,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if '--mode' not in parsed.stdout:
            raise RuntimeError('Worker smoke check failed')
        candidate = args.output.with_suffix('.tmp.zip')
        run('ditto', '-c', '-k', '--keepParent', '--norsrc', '--noextattr', bundle, candidate)
        run('unzip', '-tq', candidate, stdout=subprocess.DEVNULL)
        if args.notary_profile:
            if args.sign == '-':
                raise RuntimeError('Notarization requires Developer ID signing')
            run('xcrun', 'notarytool', 'submit', candidate, '--keychain-profile', args.notary_profile, '--wait')
            run('xcrun', 'stapler', 'staple', bundle)
            run('xcrun', 'stapler', 'validate', bundle)
            run('ditto', '-c', '-k', '--keepParent', '--norsrc', '--noextattr', bundle, candidate)
        os.replace(candidate, args.output)
        receipt = dict(format=1, artifact=str(args.output), sha256=sha(args.output), bytes=args.output.stat().st_size,
                       version=args.version, build=args.build, signing_identity=args.sign,
                       notarized=bool(args.notary_profile), native_dependencies=native, self_test=smoke,
                       bootstrap_smoke=setup, worker_help=True, no_inference_or_capture=True)
        args.output.with_suffix('.receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({k:v for k,v in receipt.items() if k!='native_dependencies'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
