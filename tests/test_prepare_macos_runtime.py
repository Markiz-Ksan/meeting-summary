"""Bounded software checks; no model, audio, network or host runtime operations."""
import hashlib
import importlib.util
import io
import json
import ssl
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/prepare_macos_runtime.py'
spec = importlib.util.spec_from_file_location('runtime_factory_under_test', SCRIPT)
factory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(factory)


class RuntimeFactoryChecks(unittest.TestCase):
    def test_repository_plan_matches_every_locked_software_payload(self):
        plan = json.loads((factory.PROJECT/'packaging/runtime-factory.lock.json').read_text())
        locked = factory.requirements(factory.PROJECT/'runtime/requirements.lock')
        mlx = json.loads((factory.PROJECT/'packaging/runtime-wheels.lock.json').read_text())
        factory.validate_plan(plan, locked, mlx)
        self.assertEqual(len(plan['wheels']), 57)
        self.assertEqual(plan['python']['bytes']+plan['notices']['bytes']+sum(e['bytes'] for e in plan['wheels']), 175572946)

    def test_ditto_utf8_names_without_utf8_flag_can_be_verified(self):
        name = 'Сводка встречи.app/Contents/Info.plist'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(name, b'fixture')
        data = bytearray(stream.getvalue())
        for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
            position = data.index(signature)+offset
            flags = int.from_bytes(data[position:position+2], 'little') & ~0x800
            data[position:position+2] = flags.to_bytes(2, 'little')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertNotIn(name, archive.namelist())
        with zipfile.ZipFile(io.BytesIO(data), metadata_encoding='utf-8') as archive:
            self.assertEqual(archive.read(name), b'fixture')

    def test_only_compatible_arm_wheels_are_selected(self):
        for name in ('x-1-cp312-cp312-macosx_26_0_arm64.whl',
                     'x-1-cp312-cp312-macosx_14_0_x86_64.whl',
                     'x-1-cp311-cp311-macosx_14_0_arm64.whl',
                     'x-1-cp312-cp312-win_arm64.whl'):
            self.assertIsNone(factory.wheel_rank(name), name)
        self.assertIsNotNone(factory.wheel_rank('x-1-cp310-abi3-macosx_10_9_universal2.whl'))
        self.assertIsNotNone(factory.wheel_rank('x-1-py3-none-any.whl'))
        self.assertLess(factory.wheel_rank('x-1-cp312-cp312-macosx_14_0_arm64.whl'),
                        factory.wheel_rank('x-1-py3-none-any.whl'))

    def test_failed_digest_never_becomes_a_verified_download(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)/'asset'
            entry = dict(url='https://files.pythonhosted.org/example.whl', bytes=3, sha256=hashlib.sha256(b'good').hexdigest())
            with patch.object(factory.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaisesRegex(ValueError, 'digest'):
                    factory.download(entry, destination)
            self.assertFalse(destination.exists())

    def test_tls_always_keeps_certificate_and_hostname_verification(self):
        with patch.object(factory.urllib.request, 'urlopen', return_value=io.BytesIO(b'ok')) as opened:
            with factory.open_url('https://files.pythonhosted.org/example.whl', timeout=1):
                pass
        context = opened.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_archive_traversal_and_escaping_symlink_are_refused(self):
        for name, link in (('../outside', None), ('python/escape', '/tmp/outside')):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                archive = Path(folder)/'python.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    item = tarfile.TarInfo(name)
                    if link:
                        item.type = tarfile.SYMTYPE;item.linkname = link
                        output.addfile(item)
                    else:
                        item.size = 3;output.addfile(item, io.BytesIO(b'bad'))
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    factory.extract_python(archive, Path(folder)/'unpack')
                self.assertFalse((Path(folder)/'outside').exists())

    def test_existing_output_is_preserved_before_any_network(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'runtime';target.mkdir();canary = target/'user-file';canary.write_text('keep')
            with patch.object(factory.urllib.request, 'urlopen', side_effect=AssertionError('network used')):
                with self.assertRaisesRegex(ValueError, 'exist'):
                    factory.prepare(target, {})
            self.assertEqual(canary.read_text(), 'keep')

    def test_plan_must_match_locked_versions_and_trusted_sources(self):
        requirements = {'numpy': '2.5.3'}
        entry = dict(package='numpy', version='2.5.3', filename='numpy-2.5.3-cp312-cp312-macosx_14_0_arm64.whl',
                     url='https://files.pythonhosted.org/example.whl', bytes=3, sha256='a'*64)
        plan = dict(format=1, python=factory.PYTHON_ARCHIVE.copy(), notices=factory.PYTHON_NOTICES.copy(), wheels=[entry])
        factory.validate_plan(plan, requirements, [])
        plan['wheels'][0]['url'] = 'https://untrusted.invalid/example.whl'
        with self.assertRaisesRegex(ValueError, 'source'):
            factory.validate_plan(plan, requirements, [])
        plan['wheels'][0]['url'] = entry['url'] = 'https://files.pythonhosted.org/example.whl'
        plan['wheels'][0]['version'] = '0.0.1'
        with self.assertRaisesRegex(ValueError, 'locked'):
            factory.validate_plan(plan, requirements, [])


if __name__ == '__main__':
    unittest.main()
