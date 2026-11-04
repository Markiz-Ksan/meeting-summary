"""One verified ZIP of the installed app/runtime and useful project files."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import zipfile
import uuid

WORKERS = ('worker.py','summary_writer.py','transcript_log.py','recent_transcript.py')

def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()

def verify_signature(path):
    subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(path)],check=True)

def verify_backup(path):
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)):
            raise ValueError('Duplicate archive entries')
        manifest=json.loads(archive.read('manifest.json'))
        if set(names)!=set(manifest['files'])|{'manifest.json'}:
            raise ValueError('Archive inventory mismatch')
        for name,info in manifest['files'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('Unsafe archive path')
            with archive.open(name) as stream:
                actual=hashlib.file_digest(stream,'sha256').hexdigest()
            if actual!=info['sha256']:
                raise ValueError('Archive hash mismatch: '+name)
        return manifest

def create_backup(project,app,workers,output,*,verify_app=verify_signature):
    project,app,workers,output=map(Path,(project,app,workers,output))
    verify_app(app)
    sources={}
    for prefix,folder in [('app',app)]:
        for file in folder.rglob('*'):
            if file.is_file() and not file.is_symlink():sources[prefix+'/'+file.relative_to(folder).as_posix()]=file
    for module in workers.glob('*.py'):sources['runtime/'+module.name]=module
    for name in ['src','scripts','docs','assets','tests','linux','packaging','.github']:
        for file in (project/name).rglob('*'):
            if file.is_file() and not file.is_symlink() and '__pycache__' not in file.parts and file.name!='.DS_Store' and file.suffix!='.whl' and not any(part in file.parts for part in ('.build.noindex','.venv','runtime-wheels')):
                sources['project/'+file.relative_to(project).as_posix()]=file
    for file in [*project.glob('*.md'),project/'build.sh',project/'.gitignore',project/'signing-identity.txt',project/'runtime/requirements.lock']:
        if file.is_file() and not file.is_symlink():sources['project/'+file.relative_to(project).as_posix()]=file
    manifest={'schema':1,'files':{},'installed_app':str(app),'installed_workers':str(workers),
              'notes':'Models, virtualenvs, compiler caches and meeting data are excluded. Restore runtime modules from runtime/, not experimental project/src.'}
    info=app/'Contents/Info.plist'
    if info.is_file():
        data=plistlib.loads(info.read_bytes());manifest['version']=data.get('CFBundleShortVersionString');manifest['build']=data.get('CFBundleVersion')
    output.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.backup-',suffix='.tmp',dir=output.parent);os.close(fd)
    temporary=Path(name)
    try:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
            for name,file in sorted(sources.items()):
                before=digest(file)
                archive.write(file,name)
                if digest(file)!=before:raise ValueError('Source changed during backup: '+str(file))
                manifest['files'][name]={'sha256':before,'mode':file.stat().st_mode & 0o777}
            archive.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
        verify_backup(temporary)
        with temporary.open('rb') as stream:os.fsync(stream.fileno())
        os.replace(temporary,output)
        return manifest
    finally:
        temporary.unlink(missing_ok=True)

def retire_installer_backups(app,workers,archive,trash):
    with (Path(app).parent/'.meeting-overlay-install.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _retire_installer_backups(app,workers,archive,trash)

def _retire_installer_backups(app,workers,archive,trash):
    app,workers,trash=map(Path,(app,workers,trash))
    manifest=verify_backup(archive)
    def matches(bundle,modules):
        pairs=[(bundle/'Contents/MacOS/MeetingOverlay','app/Contents/MacOS/MeetingOverlay')]
        pairs += [(modules/Path(n).name,n) for n in manifest['files'] if n.startswith('runtime/')]
        return all(p.is_file() and digest(p)==manifest['files'].get(n,{}).get('sha256') for p,n in pairs)
    candidates=[]
    for root in app.parent.glob('.meeting-install-*'):
        receipt=root/'receipt.json'
        if root.is_symlink() or not receipt.is_file():continue
        record=json.loads(receipt.read_text())
        if record.get('app_destination')!=str(app) or record.get('worker_destination')!=str(workers):continue
        worker_backup=record.get('worker_backup')
        if not worker_backup:continue
        partner=Path(worker_backup).parent
        if partner.parent!=workers.parent or not partner.name.startswith('.meeting-install-') or partner.is_symlink():continue
        if any((r/n).exists() for r in (root,partner) for n in ('new','rejected')):continue
        if not (root/'previous/Contents/MacOS/MeetingOverlay').is_file() or not (partner/'previous/worker.py').is_file():continue
        candidates.append((root,partner))
    if not (matches(app,workers) or any(matches(a/'previous',b/'previous') for a,b in candidates)):
        raise ValueError('Backup does not cover the installed or previous runtime; keep recovery directories')
    moved=[]
    for roots in candidates:
        for root in roots:
            if not root.exists():continue
            trash.mkdir(parents=True,exist_ok=True)
            target=trash/root.name
            if target.exists():raise FileExistsError(target)
            shutil.move(str(root),str(target))
            moved.append({'from':str(root),'to':str(target)})
    return moved

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--app',type=Path,default=Path.home()/'Applications/Сводка встречи.app')
    parser.add_argument('--workers',type=Path,default=Path.home()/'Library/Application Support/MeetingOverlay/src')
    parser.add_argument('--verify-only',action='store_true')
    parser.add_argument('--retire-installer-backups',action='store_true')
    args=parser.parse_args();target=args.project/'.backups/last-working.zip'
    if args.retire_installer_backups and not args.verify_only:parser.error('Retirement requires --verify-only')
    manifest=verify_backup(target) if args.verify_only else create_backup(args.project,args.app,args.workers,target)
    moved=retire_installer_backups(args.app,args.workers,target,Path.home()/'.Trash'/('MeetingOverlay-retired-'+uuid.uuid4().hex[:8])) if args.retire_installer_backups else []
    print(json.dumps({'backup':str(target),'version':manifest.get('version'),'files':len(manifest['files']),'bytes':target.stat().st_size,'retired':moved}))

if __name__=='__main__':main()
