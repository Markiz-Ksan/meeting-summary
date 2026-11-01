import importlib.util
import fcntl
from pathlib import Path
import shutil
import plistlib
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install_release.py'
spec = importlib.util.spec_from_file_location('install_release', SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'release.app'
        self.destination = root / 'Applications/Сводка встречи.app'
        self.workers = root / 'source'
        self.runtime = root / 'Runtime/src'
        for path in (self.source, self.destination, self.workers, self.runtime):
            path.mkdir(parents=True)
        (self.source / 'version').write_text('new')
        (self.destination / 'version').write_text('old')
        for name in installer.WORKER_FILES:
            (self.workers / name).write_text('new-' + name)
            (self.runtime / name).write_text('old-' + name)
        (self.runtime / 'obsolete.py').write_text('old-only')

    def install(self, **kwargs):
        return installer.install_release(
            self.source, self.destination, self.workers, self.runtime,
            verify=kwargs.pop('verify', lambda _: None),
            copy_app=kwargs.pop('copy_app', shutil.copytree), **kwargs)

    def assert_old(self):
        self.assertEqual((self.destination / 'version').read_text(), 'old')
        for name in installer.WORKER_FILES:
            self.assertEqual((self.runtime / name).read_text(), 'old-' + name)
        self.assertTrue((self.runtime / 'obsolete.py').is_file())

    def test_success_installs_exact_workers_and_retains_both_backups(self):
        receipt = self.install()
        self.assertEqual((self.destination / 'version').read_text(), 'new')
        self.assertEqual(set(p.name for p in self.runtime.iterdir()), set(installer.WORKER_FILES))
        for name in installer.WORKER_FILES:
            self.assertEqual((self.runtime / name).read_bytes(), (self.workers / name).read_bytes())
        self.assertEqual((Path(receipt['app_backup']) / 'version').read_text(), 'old')
        self.assertEqual((Path(receipt['worker_backup']) / 'obsolete.py').read_text(), 'old-only')
        self.assertTrue(Path(receipt['receipt']).is_file())

    def test_partial_copy_failure_preserves_originals(self):
        def fail_copy(_, destination):
            destination.mkdir()
            (destination / 'partial').write_text('broken')
            raise OSError('copy failed')
        with self.assertRaisesRegex(OSError, 'copy failed'):
            self.install(copy_app=fail_copy)
        self.assert_old()

    def test_stage_signature_failure_preserves_originals(self):
        def fail(_):
            raise ValueError('bad staged signature')
        with self.assertRaisesRegex(ValueError, 'bad staged signature'):
            self.install(verify=fail)
        self.assert_old()

    def test_final_signature_failure_restores_both_components(self):
        def verify(path):
            if path == self.destination:
                # Prove the failure occurs after both new components landed.
                self.assertEqual((path / 'version').read_text(), 'new')
                self.assertEqual((self.runtime / 'worker.py').read_text(), 'new-worker.py')
                raise ValueError('bad installed signature')
        with self.assertRaisesRegex(ValueError, 'bad installed signature'):
            self.install(verify=verify)
        self.assert_old()

    def test_worker_replacement_failure_restores_old_app_and_workers(self):
        def replace(source, destination):
            if destination == self.runtime and source.name == 'new':
                raise OSError('worker replace failed')
            source.rename(destination)
        with self.assertRaisesRegex(OSError, 'worker replace failed'):
            self.install(replace=replace)
        self.assert_old()

    def test_first_install_failure_removes_incomplete_destinations(self):
        shutil.rmtree(self.destination)
        shutil.rmtree(self.runtime)
        def verify(path):
            if path == self.destination:
                raise ValueError('first install rejected')
        with self.assertRaisesRegex(ValueError, 'first install rejected'):
            self.install(verify=verify)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.runtime.exists())

    def test_failed_rollback_retains_recovery_material_and_names_paths(self):
        def verify(path):
            if path == self.destination:
                raise ValueError('post install rejection')
        def replace(source, destination):
            if source.name == 'previous' and destination == self.destination:
                raise OSError('rollback blocked')
            source.rename(destination)
        with self.assertRaises(installer.RollbackError) as caught:
            self.install(verify=verify, replace=replace)
        self.assertIn('post install rejection', str(caught.exception))
        self.assertIn('rollback blocked', str(caught.exception))
        recovery = caught.exception.recovery_paths
        self.assertTrue(recovery)
        for path in recovery:
            self.assertTrue(path.exists())
            self.assertIn(str(path), str(caught.exception))
        self.assertTrue(any((path / 'previous/version').is_file() for path in recovery))
        self.assertEqual((self.runtime / 'worker.py').read_text(), 'old-worker.py')

    def test_missing_worker_module_fails_before_mutating_app(self):
        (self.workers / 'summary_writer.py').unlink()
        with self.assertRaises(FileNotFoundError):
            self.install()
        self.assert_old()

    def test_source_destination_overlap_is_rejected(self):
        with self.assertRaises(ValueError):
            installer.install_release(self.source, self.source, self.workers, self.runtime,
                                      verify=lambda _: None, copy_app=shutil.copytree)
        self.assertEqual((self.source / 'version').read_text(), 'new')

    def test_interruption_immediately_after_backup_move_cannot_delete_old_app(self):
        interrupted = False
        def replace(source, destination):
            nonlocal interrupted
            source.rename(destination)
            if source == self.destination and not interrupted:
                interrupted = True
                raise KeyboardInterrupt('interrupted after backup rename')
        with self.assertRaises(KeyboardInterrupt):
            self.install(replace=replace)
        self.assert_old()

    def test_interruption_immediately_after_first_install_move_removes_new_app(self):
        shutil.rmtree(self.destination)
        shutil.rmtree(self.runtime)
        interrupted = False
        def replace(source, destination):
            nonlocal interrupted
            source.rename(destination)
            if destination == self.destination and not interrupted:
                interrupted = True
                raise KeyboardInterrupt('interrupted after new rename')
        with self.assertRaises(KeyboardInterrupt):
            self.install(replace=replace)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.runtime.exists())

    def test_concurrent_installer_lock_preserves_originals(self):
        with (self.destination.parent / '.meeting-overlay-install.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.install()
        self.assert_old()

    def test_worker_corruption_after_replacement_restores_originals(self):
        def replace(source, destination):
            source.rename(destination)
            if source.name == 'new' and destination == self.runtime:
                (destination / 'worker.py').write_text('incomplete copy')
        with self.assertRaisesRegex(OSError, 'Installed worker hash mismatch'):
            self.install(replace=replace)
        self.assert_old()

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS codesign integration')
    def test_real_signature_corruption_is_detected_and_restores_old_files(self):
        # A disposable signed app, never the installed app or its identity.
        (self.source / 'version').unlink()
        contents = self.source / 'Contents'
        (contents / 'MacOS').mkdir(parents=True)
        (contents / 'Resources').mkdir()
        shutil.copyfile('/bin/echo', contents / 'MacOS/MeetingOverlay')
        (contents / 'MacOS/MeetingOverlay').chmod(0o755)
        (contents / 'Resources/note.txt').write_text('signed resource')
        (contents / 'Info.plist').write_bytes(plistlib.dumps({
            'CFBundleExecutable': 'MeetingOverlay',
            'CFBundleIdentifier': 'test.meeting-overlay.rollback',
            'CFBundlePackageType': 'APPL',
        }))
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(self.source)],
                       check=True, capture_output=True)
        shutil.rmtree(self.destination)
        installer.copy_bundle(self.source, self.destination)
        old_resource = self.destination / 'Contents/Resources/note.txt'
        old_resource.write_text('previous signed resource')
        subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(self.destination)],
                       check=True, capture_output=True)
        def replace(source, destination):
            source.rename(destination)
            if source.name == 'new' and destination == self.destination:
                (destination / 'Contents/Resources/note.txt').write_text('corrupted after replacement')
        def verify(path):
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(path)],
                           check=True, capture_output=True)
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.install(verify=verify, copy_app=installer.copy_bundle, replace=replace)
        self.assertIn(str(self.destination), caught.exception.cmd)
        verify(self.destination)
        self.assertEqual(old_resource.read_text(), 'previous signed resource')
        for name in installer.WORKER_FILES:
            self.assertEqual((self.runtime / name).read_text(), 'old-' + name)


if __name__ == '__main__':
    unittest.main()
