import importlib.util,json,tempfile,unittest,zipfile,shutil,fcntl
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('project_backup',Path(__file__).resolve().parents[1]/'scripts/project_backup.py')
backup=importlib.util.module_from_spec(spec)
if spec.loader and spec.origin and Path(spec.origin).exists():spec.loader.exec_module(backup)

class BackupTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.project=self.root/'project';self.app=self.root/'app';self.workers=self.root/'workers'
  for p in [self.project/'src',self.project/'runtime/models',self.app/'Contents/MacOS',self.workers]:p.mkdir(parents=True)
  (self.project/'src/main.py').write_text('working source')
  (self.project/'.gitignore').write_text('.backups/')
  (self.project/'signing-identity.txt').write_text('public certificate fingerprint')
  (self.project/'runtime/models/weights').write_text('do not copy')
  (self.project/'src/external').symlink_to(self.project/'runtime/models/weights')
  (self.app/'Contents/MacOS/MeetingOverlay').write_text('signed app bytes')
  for n in ['worker.py','summary_writer.py','transcript_log.py','recent_transcript.py']:(self.workers/n).write_text(n)
  self.output=self.project/'.backups/last-working.zip'

 def create(self):
  return backup.create_backup(self.project,self.app,self.workers,self.output,verify_app=lambda _:None)

 def test_selected_source_and_installed_runtime_without_models_or_external_symlinks(self):
  self.create();manifest=backup.verify_backup(self.output)
  self.assertIn('project/src/main.py',manifest['files'])
  self.assertIn('runtime/worker.py',manifest['files'])
  self.assertIn('project/.gitignore',manifest['files'])
  self.assertIn('project/signing-identity.txt',manifest['files'])
  self.assertFalse(any('weights' in n or 'external' in n for n in manifest['files']))
  self.assertEqual(self.output.stat().st_mode & 0o777,0o600)

 def test_failed_archive_validation_keeps_previous_backup(self):
  self.create();before=self.output.read_bytes()
  with patch.object(backup,'verify_backup',side_effect=ValueError('bad archive')):
   with self.assertRaises(ValueError):self.create()
  self.assertEqual(self.output.read_bytes(),before)
  self.assertEqual(list(self.output.parent.glob('*.tmp')),[])

 def test_corrupted_member_is_rejected(self):
  self.create()
  with zipfile.ZipFile(self.output) as z:contents={n:z.read(n) for n in z.namelist()}
  contents['runtime/worker.py']=b'tampered'
  with zipfile.ZipFile(self.output,'w') as z:
   for n,data in contents.items():z.writestr(n,data)
  with self.assertRaises(ValueError):backup.verify_backup(self.output)

 def old_install(self):
  root=self.root/'.meeting-install-app';partner=self.root/'.meeting-install-workers'
  shutil.copytree(self.app,root/'previous');shutil.copytree(self.workers,partner/'previous')
  (root/'receipt.json').write_text(json.dumps({'app_destination':str(self.app),'worker_destination':str(self.workers),'worker_backup':str(partner/'previous')}))
  return root,partner

 def test_retirement_is_recoverable_and_needs_a_matching_verified_backup(self):
  self.create();root,partner=self.old_install()
  moved=backup.retire_installer_backups(self.app,self.workers,self.output,self.root/'Trash')
  self.assertEqual(len(moved),2)
  self.assertFalse(root.exists());self.assertFalse(partner.exists())
  self.assertTrue((self.root/'Trash'/root.name/'previous/Contents/MacOS/MeetingOverlay').is_file())
  self.assertTrue((self.app/'Contents/MacOS/MeetingOverlay').is_file())
  backup.verify_backup(self.output)

 def test_uncovered_install_is_not_retired(self):
  self.create();(self.app/'Contents/MacOS/MeetingOverlay').write_text('unbacked version')
  root,partner=self.old_install()
  with self.assertRaises(ValueError):backup.retire_installer_backups(self.app,self.workers,self.output,self.root/'Trash')
  self.assertTrue(root.exists());self.assertTrue(partner.exists())

 def test_running_installer_prevents_retirement(self):
  self.create();root,partner=self.old_install()
  with (self.app.parent/'.meeting-overlay-install.lock').open('a') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
   with self.assertRaises(BlockingIOError):backup.retire_installer_backups(self.app,self.workers,self.output,self.root/'Trash')
  self.assertTrue(root.exists());self.assertTrue(partner.exists())
