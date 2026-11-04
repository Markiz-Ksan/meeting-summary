"""Pinned, SHA-256 checked downloads. Only model artifacts ever leave this module."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import ssl
import urllib.request

@dataclass(frozen=True)
class ModelFile:
    repo: str
    revision: str
    filename: str
    sha256: str
    size: int


def manifest():
    return json.loads((Path(__file__).with_name('models.json')).read_text())


def cache_root():
    return Path(os.environ.get('XDG_DATA_HOME', Path.home()/'.local/share')) / 'meeting-summary/models'


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4*1024*1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _open(url, offset):
    import certifi
    request = urllib.request.Request(url, headers={'User-Agent': 'MeetingSummary-Linux/0.7',
                                                 **({'Range': f'bytes={offset}-'} if offset else {})})
    response = urllib.request.urlopen(request, timeout=90,
                                     context=ssl.create_default_context(cafile=certifi.where()))
    if offset and response.status != 206:
        response.close()
        raise OSError('Server did not honor resumable download; remove .part to retry')
    return response


def ensure_file(directory, spec, *, opener=None, progress=None):
    if Path(spec.filename).name != spec.filename or spec.filename in ('.', '..'):
        raise ValueError('Invalid model filename')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = directory / spec.filename
    if target.exists():
        if target.stat().st_size == spec.size and digest(target) == spec.sha256:
            return target
        raise ValueError(f'Повреждена модель {target}; исходный файл сохранён')
    partial = target.with_suffix(target.suffix+'.part')
    offset = partial.stat().st_size if partial.exists() else 0
    if offset > spec.size:
        raise ValueError('Partial model is larger than expected')
    if offset < spec.size:
        url = f'https://huggingface.co/{spec.repo}/resolve/{spec.revision}/{spec.filename}?download=true'
        with (opener or _open)(url, offset) as response, partial.open('ab') as stream:
            while chunk := response.read(4*1024*1024):
                offset += len(chunk)
                if offset > spec.size:
                    raise ValueError('Model download exceeds pinned size')
                stream.write(chunk)
                if progress:
                    progress(spec.filename, offset, spec.size)
            stream.flush()
            os.fsync(stream.fileno())
    if partial.stat().st_size != spec.size or digest(partial) != spec.sha256:
        raise ValueError('Model download SHA-256 mismatch; partial file retained for inspection')
    os.replace(partial, target)
    return target


def ensure_models(progress=None, root=None):
    root = Path(root or cache_root())
    files = {}
    for group, info in manifest().items():
        for spec in info['files']:
            model = ModelFile(info['repo'], info['revision'], **spec)
            files[spec['filename']] = ensure_file(root/group, model, progress=progress)
    return files
