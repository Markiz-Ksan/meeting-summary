import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import io
from unittest.mock import patch
import unittest

spec = importlib.util.spec_from_file_location('bootstrap_under_test', Path(__file__).with_name('bootstrap.py'))
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def test_repack_reorders_bytes_and_validates_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = {'a': {'dtype': 'U8', 'shape': [3], 'data_offsets': [0, 3]},
                   'b': {'dtype': 'U8', 'shape': [2], 'data_offsets': [3, 5]}}
            new = {'a': {'dtype': 'U8', 'shape': [3], 'data_offsets': [2, 5]},
                   'b': {'dtype': 'U8', 'shape': [2], 'data_offsets': [0, 2]}}
            encoded = json.dumps(old).encode()
            (root/'source').write_bytes(struct.pack('<Q', len(encoded))+encoded+b'ABCDE')
            header = json.dumps(new).encode()
            (root/'header').write_bytes(header)
            bootstrap.repack(root/'source', root/'header', root/'target')
            self.assertEqual((root/'target').read_bytes(), struct.pack('<Q', len(header))+header+b'DEABC')
            new['a']['shape'] = [7]
            (root/'header').write_text(json.dumps(new))
            with self.assertRaisesRegex(ValueError, 'форма'):
                bootstrap.repack(root/'source', root/'header', root/'target')

    def test_download_resumes_verified_partial_and_handles_ignored_range(self):
        class Response(io.BytesIO):
            def __init__(self, body, status, headers):
                super().__init__(body);self.status = status;self.headers = headers
        for honors_range in (True, False):
            with self.subTest(honors_range=honors_range), tempfile.TemporaryDirectory() as directory:
                target = Path(directory)/'weights'
                target.with_name('weights.download').write_bytes(b'abc')
                body = b'abcdefghij'
                entry = dict(name='weights', url='https://example.invalid/pinned/weights',
                             download_size=len(body), download_sha256=hashlib.sha256(body).hexdigest())
                def fetch(request, timeout):
                    self.assertEqual(request.get_header('Range'), 'bytes=3-')
                    return Response(body[3:] if honors_range else body,
                                    206 if honors_range else 200,
                                    {'Content-Range': 'bytes 3-9/10'} if honors_range else {})
                with patch.object(bootstrap.urllib.request, 'urlopen', fetch):
                    partial = bootstrap.download(entry, target)
                self.assertEqual(partial.read_bytes(), body)

    def test_offline_setup_stages_exact_worker_preserves_canonical_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory);resources = base/'resources';root = base/'home'
            (resources/'workers').mkdir(parents=True)
            worker = b'print("fixture")\n'
            (resources/'workers/worker.py').write_bytes(worker)
            (resources/'weights').write_bytes(b'model-fixture')
            entry = dict(name='model.bin', size=13, sha256=hashlib.sha256(b'model-fixture').hexdigest(), bundled='weights')
            profile = {'label': 'fixture', 'models': [{'directory': 'summary-9b', 'files': [entry]}]}
            (resources/'models.lock.json').write_text(json.dumps({'profiles': {'standard': profile}}))
            (resources/'bundle-manifest.json').write_text(json.dumps({'worker_id': 'abc', 'workers': {'worker.py': hashlib.sha256(worker).hexdigest()}}))
            canonical = root/'src';canonical.mkdir(parents=True);(canonical/'worker.py').write_text('user-canary')
            first = bootstrap.prepare(resources, root, offline=True)
            second = bootstrap.prepare(resources, root, offline=True)
            self.assertEqual(first, second)
            self.assertEqual((canonical/'worker.py').read_text(), 'user-canary')
            self.assertEqual(Path(first['worker']).read_bytes(), worker)
            self.assertEqual((root/'portable/abc-standard/runtime/models').resolve(), Path(first['models']).resolve())
            self.assertTrue(first['models_verified'])


if __name__ == '__main__':
    unittest.main()
