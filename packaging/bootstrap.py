#!/usr/bin/env python3
"""Prepare private local assets; never opens recordings or sends meeting content.
All remote requests are HTTPS GETs to the exact immutable URLs in models.lock.json.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request


def emit(event, **values):
    print(json.dumps(dict(event=event, **values), ensure_ascii=False), flush=True)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(8 * 1024**2), b''):
            h.update(block)
    return h.hexdigest()


def valid(path, size, sha):
    return path.is_file() and path.stat().st_size == size and digest(path) == sha


def repack(source, header_path, target):
    """Reorder tensor bytes to the verified deployed layout without loading MLX."""
    header = header_path.read_bytes()
    wanted = json.loads(header)
    with source.open('rb') as incoming, target.open('wb') as outgoing:
        length = struct.unpack('<Q', incoming.read(8))[0]
        if length > 16 * 1024**2:
            raise ValueError('Некорректный заголовок весов')
        original = json.loads(incoming.read(length))
        if wanted.keys() != original.keys():
            raise ValueError('Изменился состав тензоров')
        outgoing.write(struct.pack('<Q', len(header)))
        outgoing.write(header)
        tensors = sorted(((k, v) for k, v in wanted.items() if k != '__metadata__'),
                         key=lambda item: item[1]['data_offsets'][0])
        position = 0
        for name, metadata in tensors:
            other = original[name]
            if any(metadata[k] != other[k] for k in ('shape', 'dtype')):
                raise ValueError('Изменились тип или форма тензора')
            begin, end = metadata['data_offsets']
            old_begin, old_end = other['data_offsets']
            if begin != position or end-begin != old_end-old_begin:
                raise ValueError('Некорректные границы тензора')
            incoming.seek(8 + length + old_begin)
            remaining = end-begin
            while remaining:
                block = incoming.read(min(8 * 1024**2, remaining))
                if not block:
                    raise ValueError('Неполные веса модели')
                outgoing.write(block)
                remaining -= len(block)
            position = end
        outgoing.flush()
        os.fsync(outgoing.fileno())


def download(entry, target):
    partial = target.with_name(target.name + '.download')
    total = entry['download_size']
    expected = entry['download_sha256']
    for attempt in range(3):
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > total:
            partial.unlink()
            offset = 0
        if offset == total and valid(partial, total, expected):
            return partial
        headers = {'User-Agent': 'MeetingOverlay-AssetSetup/1', 'Accept-Encoding': 'identity'}
        if offset:
            headers['Range'] = f'bytes={offset}-'
        try:
            request = urllib.request.Request(entry['url'], headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                # A server ignoring Range sends the whole file; replace rather than append.
                append = offset > 0 and response.status == 206
                if append and not response.headers.get('Content-Range', '').startswith(f'bytes {offset}-'):
                    raise ValueError('Некорректный ответ продолжения загрузки')
                if not append:
                    offset = 0
                with partial.open('ab' if append else 'wb') as output:
                    last = time.monotonic()
                    while True:
                        block = response.read(4 * 1024**2)
                        if not block:
                            break
                        offset += len(block)
                        if offset > total:
                            raise ValueError('Размер загрузки превышает manifest')
                        output.write(block)
                        if time.monotonic()-last > 0.5:
                            emit('progress', text=f'Загрузка моделей: {offset/1024**3:.2f} / {total/1024**3:.2f} ГБ',
                                 file=entry['name'], received=offset, total=total)
                            last = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
            if valid(partial, total, expected):
                return partial
            partial.unlink(missing_ok=True)
            raise ValueError('Не совпала контрольная сумма загрузки')
        except (OSError, urllib.error.URLError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt+1)
    raise RuntimeError('Загрузка не завершена')


def prepare(resources, root, offline=False, seed=None, skip_models=False, profile="standard"):
    manifest_path = resources/'models.lock.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    # Preserve the exact deployed9B asset profile on existing Macs. Public pinned
    # weights are a distinct profile; their equivalence is never inferred.
    deployed_manifest = resources/'models.installed.lock.json'
    if profile == 'standard' and deployed_manifest.is_file():
        deployed_bytes = deployed_manifest.read_bytes()
        deployed = json.loads(deployed_bytes)
        if all(valid(root/'runtime/models'/model['directory']/entry['name'], entry['size'], entry['sha256'])
               for model in deployed['profiles']['standard']['models'] for entry in model['files']):
            manifest_bytes, manifest = deployed_bytes, deployed
    manifest['models'] = manifest['profiles'][profile]['models']
    emit('progress', text='Модель сводки: '+manifest['profiles'][profile]['label'])
    bundle = json.loads((resources/'bundle-manifest.json').read_text())
    asset_root = root/'assets'/(hashlib.sha256(manifest_bytes).hexdigest()[:20]+'-'+profile)
    models = asset_root/'models'
    models.mkdir(parents=True, exist_ok=True)
    with (asset_root/'.setup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Подготовка моделей уже выполняется в другом экземпляре приложения')
        # Existing canonical models are read-only candidates; never overwritten.
        candidates = [resources/'models']
        if seed:
            candidates.append(seed)
        candidates.append(root/'runtime/models')
        if not skip_models:
            needed = sum(f['size'] for m in manifest['models'] for f in m['files']
                         if not (models/m['directory']/f['name']).exists())
            # Repacking the 5GB shard needs one extra source copy, plus margin.
            if shutil.disk_usage(asset_root).free < needed + 6 * 1024**3:
                raise RuntimeError('Для подготовки моделей нужно не менее 13 ГБ свободного места')
            for model in manifest['models']:
                directory = models/model['directory']
                directory.mkdir(exist_ok=True)
                for entry in model['files']:
                    destination = directory/entry['name']
                    emit('progress', text='Проверка моделей: '+entry['name'])
                    if valid(destination, entry['size'], entry['sha256']):
                        continue
                    if destination.exists():
                        # Keep damaged files as evidence; no unknown files are deleted.
                        destination.rename(destination.with_name(destination.name+f'.invalid-{time.time_ns()}'))
                    staged = destination.with_name(destination.name+'.preparing')
                    bundled = resources/entry['bundled'] if 'bundled' in entry else None
                    source = bundled
                    if not source:
                        source = next((base/model['directory']/entry['name'] for base in candidates
                                       if valid(base/model['directory']/entry['name'], entry['size'], entry['sha256'])), None)
                    if source:
                        staged.unlink(missing_ok=True)
                        if not bundled:
                            try:
                                os.link(source, staged)  # Keep host weights intact, without another physical copy.
                            except OSError:
                                shutil.copyfile(source, staged)
                        else:
                            shutil.copyfile(source, staged)
                    else:
                        if offline:
                            raise RuntimeError('Модели ещё не загружены: '+entry['name'])
                        incoming = download(entry, destination)
                        if 'repack_header' in entry:
                            emit('progress', text='Подготовка совместимых весов Qwen…')
                            repack(incoming, resources/entry['repack_header'], staged)
                        else:
                            os.replace(incoming, staged)
                    if not valid(staged, entry['size'], entry['sha256']):
                        staged.unlink(missing_ok=True)
                        raise ValueError('Не совпала итоговая контрольная сумма: '+entry['name'])
                    os.replace(staged, destination)
                    destination.with_name(destination.name+'.download').unlink(missing_ok=True)
        worker_root = root/'portable'/(bundle['worker_id']+'-'+profile)
        worker_source = worker_root/'src'
        worker_source.mkdir(parents=True, exist_ok=True)
        for name, expected in bundle['workers'].items():
            source = resources/'workers'/name
            if digest(source) != expected:
                raise ValueError('Повреждён модуль приложения: '+name)
            target = worker_source/name
            if not target.exists() or digest(target) != expected:
                staged = target.with_suffix('.preparing')
                shutil.copyfile(source, staged)
                os.replace(staged, target)
        runtime = worker_root/'runtime'
        runtime.mkdir(exist_ok=True)
        link = runtime/'models'
        if link.is_symlink() and link.resolve() != models.resolve():
            pending_link = runtime/'models.preparing'
            pending_link.unlink(missing_ok=True)
            pending_link.symlink_to(models, target_is_directory=True)
            os.replace(pending_link, link)
        elif link.exists() and not link.is_symlink():
            raise ValueError('Конфликт каталога моделей')
        elif not link.exists():
            link.symlink_to(models, target_is_directory=True)
        receipt = dict(format=1, worker_id=bundle['worker_id'], profile=profile, model_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                       worker=str(worker_source/'worker.py'), models=str(models), models_verified=not skip_models)
        staged = asset_root/'receipt.preparing'
        staged.write_text(json.dumps(receipt, ensure_ascii=False, indent=2)+'\n')
        os.replace(staged, asset_root/'receipt.json')
        emit('ready', **receipt)
        return receipt


def self_test(resources):
    """Import every native dependency, without allocating or loading any model."""
    import importlib
    modules = ['numpy', 'soundfile', 'scipy.signal', 'mlx.core', 'mlx_lm', 'parakeet_mlx', 'tokenizers', 'sentencepiece']
    for module in modules:
        importlib.import_module(module)
    root = Path(sys.prefix).resolve()
    external = [p for p in sys.path if p and 'site-packages' in p and not Path(p).resolve().is_relative_to(root)]
    if external:
        raise RuntimeError('Runtime использует внешние site-packages')
    emit('self_test', python=sys.version.split()[0], architecture=platform.machine(), prefix=str(root),
         modules=modules, external_site_packages=external, models_loaded=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path)
    parser.add_argument('--resources', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--profile', choices=['standard', 'light'], default='standard')
    parser.add_argument('--seed-models', type=Path)
    parser.add_argument('--skip-models', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test(args.resources)
    elif args.root:
        prepare(args.resources.resolve(), args.root.resolve(), args.offline, args.seed_models, args.skip_models, args.profile)
    else:
        parser.error('--root is required')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit('error', text=str(error))
        sys.exit(1)
