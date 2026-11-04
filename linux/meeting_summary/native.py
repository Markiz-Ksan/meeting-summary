"""Use private audio tools in frozen distributions; source runs use distro tools."""
from pathlib import Path
import os
import shutil
import sys


def executable(name):
    bundled = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'native/bin' / name
    if bundled.is_file():
        return str(bundled)
    result = shutil.which(name)
    if not result:
        raise RuntimeError(f'Не найден {name}; установите пакет meeting-summary с зависимостями')
    return result


def environment():
    env = dict(os.environ)
    native = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'native/lib'
    if native.is_dir():
        env['LD_LIBRARY_PATH'] = str(native) + (':'+env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
    return env
