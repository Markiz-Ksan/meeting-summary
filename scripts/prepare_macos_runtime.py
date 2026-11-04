#!/usr/bin/env python3
"""Prepare an isolated relocatable CPython/runtime source for package_macos.py.
Only official pinned CPython and version-locked, digest-verified PyPI wheels are
used. Existing output paths are refused. No installed MeetingOverlay is read.
"""
import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parents[1]
PYTHON_ARCHIVE = {
    'filename': 'cpython-3.12.13+20260718-aarch64-apple-darwin-install_only_stripped.tar.gz',
    'url': 'https://github.com/astral-sh/python-build-standalone/releases/download/20260718/cpython-3.12.13%2B20260718-aarch64-apple-darwin-install_only_stripped.tar.gz',
    'bytes': 25000146,
    'sha256': '9a1e9e06175c10efd8378b904b07fa21bd791ab3345d7cdffeb4a76c9ff55903',
    'source': 'https://github.com/astral-sh/python-build-standalone/releases/tag/20260718',
    'license': 'CPython PSF license and bundled dependency notices; license texts retained verbatim',
}


PYTHON_NOTICES = {
    'filename': 'python-licenses.rst',
    'url': 'https://raw.githubusercontent.com/astral-sh/python-build-standalone/20260718/python-licenses.rst',
    'bytes': 105875,
    'sha256': 'e43fb936c6655d7996dba480d7ebdea492d6040ec388eb8ed9d1000f72de8cab',
}


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def requirements(path):
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9.+_-]+)', line)
        if not match or normalized(match[1]) in result:
            raise ValueError('Requirements must contain unique exact version pins')
        result[normalized(match[1])] = match[2]
    return result


def wheel_rank(filename):
    if not filename.endswith('.whl'):
        return None
    parts = filename[:-4].split('-')
    if len(parts) < 5:
        return None
    python_tags, abi, platforms = parts[-3:]
    tags = python_tags.split('.')
    if 'cp312' in tags and ('cp312' in abi.split('.') or abi == 'abi3'):
        python_rank = 0
    elif abi == 'abi3' and any(re.fullmatch(r'cp3[0-9]+', tag) and int(tag[3:]) <= 12 for tag in tags):
        python_rank = 1
    elif abi == 'none' and any(tag in ('py3', 'py312') for tag in tags):
        python_rank = 2
    else:
        return None
    choices = []
    for tag in platforms.split('.'):
        if tag == 'any':
            choices.append((2, 0, 0))
        match = re.fullmatch(r'macosx_(\d+)_(\d+)_(arm64|universal2)', tag)
        if match and (int(match[1]), int(match[2])) <= (14, 4):
            choices.append((0 if match[3] == 'arm64' else 1, -int(match[1]), -int(match[2])))
    return (python_rank, *min(choices), filename) if choices else None


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4*1024**2), b''):
            h.update(block)
    return h.hexdigest()


def open_url(url, timeout):
    context = ssl.create_default_context()
    system_ca = Path('/private/etc/ssl/cert.pem')
    if context.cert_store_stats()['x509_ca'] == 0 and system_ca.is_file():
        context.load_verify_locations(cafile=str(system_ca))
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    request = urllib.request.Request(url, headers={'User-Agent': 'MeetingOverlay-runtime-factory/1'})
    response = urllib.request.urlopen(request, timeout=timeout, context=context)
    if urlparse(getattr(response, 'geturl', lambda: url)()).scheme != 'https':
        response.close()
        raise ValueError('Download source redirected outside HTTPS')
    return response


def build_plan(locked, mlx_lock):
    overrides = {normalized(entry['filename'].split('-')[0]): entry for entry in mlx_lock}
    def resolve(item):
        package, version = item
        if package in overrides:
            return dict(overrides[package], package=package, version=version,
                        metadata_source='packaging/runtime-wheels.lock.json')
        url = f'https://pypi.org/pypi/{package}/{version}/json'
        with open_url(url, timeout=30) as response:
            metadata = json.load(response)
        options = [entry for entry in metadata['urls'] if not entry.get('yanked') and
                   entry.get('packagetype') == 'bdist_wheel' and wheel_rank(entry['filename']) is not None]
        if not options:
            raise ValueError('No compatible binary wheel for locked package '+package+'=='+version)
        selected = min(options, key=lambda entry: wheel_rank(entry['filename']))
        return dict(package=package, version=version, filename=selected['filename'], url=selected['url'],
                    bytes=selected['size'], sha256=selected['digests']['sha256'], metadata_source=url,
                    license=(metadata['info'].get('license_expression') or metadata['info'].get('license') or 'See retained wheel notices')[:200])
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        wheels = list(pool.map(resolve, sorted(locked.items())))
    plan = dict(format=1, python=PYTHON_ARCHIVE.copy(), notices=PYTHON_NOTICES.copy(), wheels=wheels,
                note='Exact package versions; wheel URLs/digests frozen in this saved plan. No byte-identical app rebuild claim.')
    validate_plan(plan, locked, mlx_lock)
    return plan


def validate_plan(plan, locked, mlx_lock):
    if plan.get('format') != 1 or plan.get('python') != PYTHON_ARCHIVE or plan.get('notices') != PYTHON_NOTICES:
        raise ValueError('Unexpected pinned CPython source')
    entries = plan.get('wheels', [])
    if len(entries) != len(locked) or {entry['package']: entry['version'] for entry in entries} != locked:
        raise ValueError('Plan does not match locked package versions')
    overrides = {normalized(entry['filename'].split('-')[0]): entry for entry in mlx_lock}
    for entry in entries:
        url = urlparse(entry['url'])
        if url.scheme != 'https' or url.hostname != 'files.pythonhosted.org' or url.username or url.password:
            raise ValueError('Unexpected wheel source')
        if not re.fullmatch('[0-9a-f]{64}', entry['sha256']) or not 0 < entry['bytes'] <= 256*1024**2:
            raise ValueError('Invalid wheel digest/size')
        if wheel_rank(entry['filename']) is None or Path(entry['filename']).name != entry['filename']:
            raise ValueError('Incompatible wheel in locked plan')
        package = entry['package']
        if normalized(entry['filename'].split('-')[0]) != package or entry['filename'].split('-')[1] != locked[package]:
            raise ValueError('Wheel filename does not match locked package')
        if package in overrides and any(entry[key] != overrides[package][key] for key in ('filename', 'url', 'bytes', 'sha256')):
            raise ValueError('MLX must use the repository-pinned macOS14 wheels')


def download(entry, destination):
    partial = destination.with_suffix(destination.suffix+'.partial')
    try:
        with open_url(entry['url'], timeout=60) as incoming, partial.open('xb') as output:
            size = 0
            for block in iter(lambda: incoming.read(4*1024**2), b''):
                size += len(block)
                if size > entry['bytes']:
                    raise ValueError('Download exceeds pinned size')
                output.write(block)
        if size != entry['bytes'] or digest(partial) != entry['sha256']:
            raise ValueError('Download digest or size mismatch')
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def extract_python(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        if len(members) > 30000 or sum(member.size for member in members) > 512*1024**2:
            raise ValueError('Unexpected expanded Python archive size')
        if any(not member.name.startswith('python/') and member.name != 'python' for member in members):
            raise ValueError('Archive must contain only the python directory')
        source.extractall(destination, members=members, filter='data')


def prepare(output, plan):
    if output.exists() or output.is_symlink():
        raise ValueError('Output must not exist; existing paths are preserved')
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('Factory requires native Apple Silicon macOS')
    if tuple(map(int, platform.mac_ver()[0].split('.')[:2])) < (14, 4):
        raise ValueError('Factory requires macOS14.4 or newer')
    for name in ('codesign', 'install_name_tool', 'otool', 'lipo'):
        if shutil.which(name) is None:
            raise ValueError('Missing Apple build tool: '+name)
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < 3*1024**3:
        raise ValueError('Factory requires at least3GiB free disk space')
    validate_plan(plan, requirements(PROJECT/'runtime/requirements.lock'), json.loads((PROJECT/'packaging/runtime-wheels.lock.json').read_text()))
    output.mkdir()  # Exclusive creation: no installed runtime or user files are replaced.
    receipt = dict(format=1, complete=False, runtime_source=str(output), plan=plan, model_or_audio_operations=False)
    try:
        with tempfile.TemporaryDirectory(prefix='meeting-runtime-downloads-', dir=output.parent) as folder:
            cache = Path(folder)
            for entry in [plan['python'], plan['notices'], *plan['wheels']]:
                download(entry, cache/entry['filename'])
            unpack = cache/'unpacked'; extract_python(cache/plan['python']['filename'], unpack)
            shutil.move(str(unpack/'python'), output/'python')
            notices = output/'python/share/licenses/python-build-standalone'; notices.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache/plan['notices']['filename'], notices/plan['notices']['filename'])
            python = output/'python/bin/python3.12'; venv = output/'.venv'
            home = output/'.factory-home'; home.mkdir()
            environment = dict(HOME=str(home), PATH='/usr/bin:/bin:/usr/sbin:/sbin', TMPDIR=str(cache),
                               PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                               HF_HUB_DISABLE_TELEMETRY='1', TRANSFORMERS_OFFLINE='1', NUMBA_CACHE_DIR=str(cache/'numba'))
            subprocess.run([str(python), '-I', '-B', '-m', 'venv', '--copies', str(venv)], env=environment, check=True)
            hashed = cache/'requirements.hashed.txt'
            hashed.write_text(''.join(f"{entry['package']}=={entry['version']} --hash=sha256:{entry['sha256']}\n" for entry in plan['wheels']))
            interpreter = venv/'bin/python'
            subprocess.run([str(interpreter), '-I', '-B', '-m', 'pip', '--isolated', 'install', '--no-index',
                            '--no-deps', '--no-cache-dir', '--no-compile', '--require-hashes', '--find-links', str(cache),
                            '-r', str(hashed)], env=environment, check=True)
            subprocess.run([str(interpreter), '-I', '-B', '-m', 'pip', '--isolated', 'check'], env=environment, check=True)
            spec = importlib.util.spec_from_file_location('meeting_packager', PROJECT/'scripts/package_macos.py')
            packager = importlib.util.module_from_spec(spec); spec.loader.exec_module(packager)
            packager.relocate(output)
            native = packager.audit(output)
            for entry in native:
                subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(output/entry['path'])],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
            smoke = '''import importlib,importlib.metadata,json,sys
from pathlib import Path
def deny(event,args):
    if event in ('socket.connect','socket.getaddrinfo'):raise RuntimeError('Network forbidden during import smoke')
sys.addaudithook(deny)
modules=['numpy','soundfile','scipy.signal','mlx.core','mlx_lm','parakeet_mlx','tokenizers','sentencepiece']
for name in modules:importlib.import_module(name)
external=[p for p in sys.path if p and 'site-packages' in p and not Path(p).resolve().is_relative_to(Path(sys.prefix).resolve())]
assert not external
assert sys.version_info[:3]==(3,12,13)
print(json.dumps(dict(python=sys.version,architecture=__import__('platform').machine(),prefix=sys.prefix,base_prefix=sys.base_prefix,modules=modules,external_site_packages=external,models_loaded=False)))
'''
            result = subprocess.run([str(interpreter), '-I', '-B', '-c', smoke], env=environment,
                                    capture_output=True, text=True, check=True)
            receipt.update(complete=True, native_modifications='Relocated library load commands and ad-hoc signatures; no byte-identical rebuild claim', native_dependencies=native, import_smoke=json.loads(result.stdout.strip().splitlines()[-1]),
                           retained_license_files=[str(p.relative_to(output)) for p in output.rglob('*') if p.is_file() and
                                                   ('license' in p.name.lower() or p.name.lower().startswith('notice'))])
    except BaseException as error:
        receipt['error'] = type(error).__name__+': '+str(error)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            receipt['stderr_tail'] = str(error.stderr)[-6000:]
        raise
    finally:
        (output/'runtime-factory-receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New runtime-source directory; must not exist')
    parser.add_argument('--plan-output', type=Path, help='Maintenance: resolve a new wheel plan, without downloads')
    parser.add_argument('--plan', type=Path, help='Use an exact wheel plan; default: repository runtime-factory.lock.json')
    args = parser.parse_args()
    if not args.output and not args.plan_output:
        parser.error('--output or --plan-output is required')
    if args.output and (args.output.exists() or args.output.is_symlink()):
        parser.error('--output must not exist; existing paths are preserved')
    if args.plan_output and args.plan_output.exists():
        parser.error('--plan-output must not exist; choose a new review plan path')
    locked = requirements(PROJECT/'runtime/requirements.lock'); mlx = json.loads((PROJECT/'packaging/runtime-wheels.lock.json').read_text())
    if args.plan_output and not args.plan:
        plan = build_plan(locked, mlx)
    else:
        plan = json.loads((args.plan or PROJECT/'packaging/runtime-factory.lock.json').read_text())
    validate_plan(plan, locked, mlx)
    if args.plan_output:
        args.plan_output.write_text(json.dumps(plan, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(dict(plan=str(args.plan_output), archives=1, wheels=len(plan['wheels']), bytes=plan['python']['bytes']+plan['notices']['bytes']+sum(e['bytes'] for e in plan['wheels']))))
    if args.output:
        receipt = prepare(args.output.absolute(), plan)
        print(json.dumps(dict(complete=receipt['complete'], runtime_source=receipt['runtime_source'], native_files=len(receipt['native_dependencies']), licenses=len(receipt['retained_license_files']))))


if __name__ == '__main__':
    main()
