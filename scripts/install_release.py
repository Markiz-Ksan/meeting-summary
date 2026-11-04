"""Checked app/worker installation; no capture, model loading or app launch.

Directory replacement is atomic per component, not across the two directories.
Caught failures restore both components. SIGKILL/power loss may need the retained
recovery directories; this is not a crash-atomic filesystem transaction.
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile

WORKER_FILES = ('worker.py', 'recent_transcript.py', 'summary_writer.py', 'transcript_log.py', 'session_title.py')


class RollbackError(RuntimeError):
    def __init__(self, original, errors, recovery_paths):
        self.recovery_paths = recovery_paths
        super().__init__('Installation failed: ' + str(original) +
                         '; rollback incomplete: ' + '; '.join(errors) +
                         '; retained recovery directories: ' +
                         ', '.join(map(str, recovery_paths)))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_signature(path):
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(path)], check=True)


def copy_bundle(source, destination):
    subprocess.run(['/usr/bin/ditto', '--norsrc', '--noextattr', str(source), str(destination)], check=True)


def install_release(app_source, app_destination, worker_source, worker_destination,
                    *, verify=verify_signature, copy_app=copy_bundle, replace=os.replace):
    paths = [Path(p).absolute() for p in (app_source, app_destination, worker_source, worker_destination)]
    app_source, app_destination, worker_source, worker_destination = paths
    if any(path.is_symlink() for path in (app_destination, worker_destination)):
        raise ValueError('Installation destinations must not be symlinks')
    resolved = [p.resolve() for p in paths]
    if any(a == b or a in b.parents or b in a.parents
           for i, a in enumerate(resolved) for b in resolved[i + 1:]):
        raise ValueError('Release sources and destinations must not overlap')
    if not app_source.is_dir():
        raise FileNotFoundError(app_source)
    # Older verified backups have four modules. New title-capable workers must
    # include their fifth dependency; legacy restoration must remain usable.
    worker_files = tuple(name for name in WORKER_FILES if name != 'session_title.py' or (worker_source/name).is_file())
    if 'session_title.py' not in worker_files and re.search(r'(?m)^\s*(?:from\s+session_title\s+import|import\s+session_title\b)', (worker_source/'worker.py').read_text()):
        raise FileNotFoundError(worker_source/'session_title.py')
    for name in worker_files:
        if not (worker_source / name).is_file():
            raise FileNotFoundError(worker_source / name)
    for destination in (app_destination, worker_destination):
        if destination.exists() and not destination.is_dir():
            raise ValueError('Installation destination is not a directory: ' + str(destination))
        destination.parent.mkdir(parents=True, exist_ok=True)

    # Prevent two installers from moving each other's just-installed directories.
    with (app_destination.parent / '.meeting-overlay-install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        components = []
        try:
            for destination in (app_destination, worker_destination):
                root = Path(tempfile.mkdtemp(prefix='.meeting-install-', dir=destination.parent))
                components.append(dict(root=root, destination=destination,
                                       staged=root / 'new', backup=root / 'previous',
                                       install_attempted=False))
            app, worker = components
            copy_app(app_source, app['staged'])
            verify(app['staged'])
            worker['staged'].mkdir()
            hashes = {}
            for name in worker_files:
                shutil.copyfile(worker_source / name, worker['staged'] / name)
                hashes[name] = digest(worker_source / name)
                if digest(worker['staged'] / name) != hashes[name]:
                    raise OSError('Worker staging hash mismatch: ' + name)

            for component in components:
                if component['destination'].exists():
                    replace(component['destination'], component['backup'])
                component['install_attempted'] = True
                replace(component['staged'], component['destination'])

            verify(app_destination)
            if any(digest(worker_destination / name) != hashes[name] for name in worker_files):
                raise OSError('Installed worker hash mismatch')
            receipt = dict(app_destination=str(app_destination), worker_destination=str(worker_destination),
                           app_backup=str(app['backup']) if app['backup'].exists() else None,
                           worker_backup=str(worker['backup']) if worker['backup'].exists() else None,
                           worker_sha256=hashes, receipt=str(app['root'] / 'receipt.json'))
            Path(receipt['receipt']).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
            return receipt
        except BaseException as error:
            rollback_errors = []
            for component in reversed(components):
                try:
                    # A signal can arrive after rename but before Python updates
                    # a status flag. The retained directories are authoritative.
                    has_backup = component['backup'].exists()
                    has_new = component['install_attempted'] and not component['staged'].exists()
                    if (has_backup or has_new) and component['destination'].exists():
                        replace(component['destination'], component['root'] / 'rejected')
                    if has_backup:
                        replace(component['backup'], component['destination'])
                except BaseException as restore_error:
                    rollback_errors.append(str(component['destination']) + ': ' + str(restore_error))
            if rollback_errors:
                raise RollbackError(error, rollback_errors, [c['root'] for c in components]) from error
            for component in components:
                shutil.rmtree(component['root'])
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('app_source', type=Path)
    parser.add_argument('app_destination', type=Path)
    parser.add_argument('worker_source', type=Path)
    parser.add_argument('worker_destination', type=Path)
    args = parser.parse_args()
    # Ordinary termination should take the same rollback path as copy failures.
    def interrupted(signum, _frame):
        raise SystemExit('Installation interrupted by signal ' + str(signum))
    for name in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(name, interrupted)
    receipt = install_release(args.app_source, args.app_destination, args.worker_source, args.worker_destination)
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
