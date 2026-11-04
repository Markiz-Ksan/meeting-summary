#!/usr/bin/env python3
"""Bundle distro audio tools + all non-glibc dependencies and license notices."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

def main(destination):
    root = Path(destination)
    binaries, libraries = root/'bin', root/'lib'
    notices = root/'licenses'
    for folder in (binaries,libraries,notices): folder.mkdir(parents=True,exist_ok=True)
    shipped = set()
    excluded = re.compile(r'^(ld-linux|libc\.|libm\.|libdl\.|libpthread\.|librt\.|libresolv\.|libnss_|libutil\.)')
    for tool in ['ffmpeg','pactl']:
        original = Path(shutil.which(tool) or '')
        if not original.is_file(): raise RuntimeError(f'{tool} missing')
        shutil.copy2(original,binaries/tool)
        shipped.add(str(original))
        output = subprocess.check_output(['ldd',str(original)],text=True)
        if 'not found' in output: raise RuntimeError(output)
        for value in re.findall(r'(?:=>\s+)?(/[^\s()]+)',output):
            path=Path(value)
            if excluded.match(path.name):continue
            shutil.copy2(path,libraries/path.name,follow_symlinks=True)
            shipped.add(str(path))
    packages={}
    for item in sorted(shipped):
        candidates=[item,str(Path(item).resolve())]
        if item.startswith('/lib/'): candidates.append('/usr'+item)
        for candidate in candidates:
            result=subprocess.run(['dpkg-query','-S',candidate],text=True,capture_output=True)
            if result.returncode==0:
                package=result.stdout.split(': /')[0].splitlines()[0]
                packages[package]=subprocess.check_output(['dpkg-query','-W','-f=${Version}',package],text=True)
                break
    for package in packages:
        base=package.split(':')[0]
        original=Path('/usr/share/doc')/base/'copyright'
        if original.exists():shutil.copy2(original,notices/(base+'.copyright'))
    (notices/'ubuntu-packages.json').write_text(json.dumps(packages,indent=2)+'\n')
    (notices/'SOURCE.txt').write_text('Binary provenance: Ubuntu 22.04/24.04 official apt repositories at build time.\nExact package versions are in ubuntu-packages.json.\nCorresponding source packages: https://launchpad.net/ubuntu/+source/<source-package>/<version>\nOr run apt-get source <package>=<version> with matching deb-src repositories enabled.\nFFmpeg built by Ubuntu, unmodified. License notices copied alongside.\n')

if __name__=='__main__': main(sys.argv[1])
