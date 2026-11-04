"""Offline build-preview delivery; no model, device, network or release operation."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import stat
import tempfile
import unittest
import warnings
import zipfile

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/'scripts/deliver_preview.py'
if MODULE.exists():
    spec=importlib.util.spec_from_file_location('preview_delivery',MODULE)
    delivery=importlib.util.module_from_spec(spec);spec.loader.exec_module(delivery)
else: delivery=None
HEAD='a'*40
SHA='b'*64
TITLE='0.7.0 beta — build 18 — без модельных тестов'
MAC='MeetingSummary-0.7.0-beta-macos-arm64.zip'
DEB='meeting-summary_0.7.0_amd64.deb'
APP='MeetingSummary-0.7.0-x86_64.AppImage'
CHECKS=dict.fromkeys(('CAPTURE','REGRESSIONS','GUI','BUILD','INSTALL'),'success')
WORKERS={name:('# fixture '+name+'\n').encode() for name in ('worker.py','summary_writer.py','recent_transcript.py','session_title.py','transcript_log.py')}

def digest(data): return hashlib.sha256(data).hexdigest()
def encode(value): return json.dumps(value).encode()
def archive(path,files,extra=None):
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items(): z.writestr(name,data)
        if extra: z.writestr(*extra)
    return digest(path.read_bytes())

def fixtures(platform):
    if platform=='linux':
        return {'linux-artifacts/'+DEB:b'deb','linux-artifacts/'+APP:b'app',
          'linux-artifacts/SHA256SUMS':(digest(b'deb')+'  ./'+DEB+'\n'+digest(b'app')+'  ./'+APP+'\n').encode(),
          'linux-artifacts/ci-gates.json':encode(dict(checks=CHECKS,inference_requested=False,release_eligible=False)),
          'linux-capture.log':b'ignored only'}
    workers={name:digest(data) for name,data in WORKERS.items()}
    worker_id=digest(json.dumps(workers,sort_keys=True).encode())[:20]
    app=io.BytesIO()
    base='Сводка встречи.app/Contents/'
    with zipfile.ZipFile(app,'w') as z:
        z.writestr(base+'Info.plist',plistlib.dumps(dict(CFBundleVersion='18',CFBundleShortVersionString='0.7.0',LSMinimumSystemVersion='14.4')))
        z.writestr(base+'Resources/bundle-manifest.json',encode(dict(workers=workers,worker_id=worker_id,models_bundled=False)))
        for name,data in WORKERS.items(): z.writestr(base+'Resources/workers/'+name,data)
    binary=app.getvalue();sha=digest(binary)
    common=dict(commit=HEAD,build='18',model_validation='not_run',audio_validation='not_run',semantic_acceptance='not_evaluated',signing='adhoc',notarized=False)
    smoke=dict(python='3.12.13',architecture='arm64',models_loaded=False,external_site_packages=[])
    return {MAC:binary,'SHA256SUMS':(sha+'  '+MAC+'\n').encode(),
      'runtime-factory-receipt.json':encode(dict(common,complete=True,import_smoke=smoke,native_dependencies=['lib'],retained_license_files=['LICENSE'])),
      'package-receipt.json':encode(dict(common,version='0.7.0',worker_id=worker_id,workers=workers,worker_help=True,import_smoke=smoke)),
      'saved-archive-receipt.json':encode(dict(common,artifact=MAC,sha256=sha,bytes=len(binary),signature_verified=True,minimum_macos='14.4',native_dependencies=['lib'],network_during_import_check='denied',import_smoke=smoke,workers=workers))}

def provenance(platform):
    repo=dict(id=1442233273,full_name='Markiz-Ksan/meeting-summary',private=True)
    run=dict(id=123,run_number=9,status='completed',conclusion='success',head_branch='main',head_sha=HEAD,
             path='.github/workflows/'+('macos-package.yml' if platform=='macos' else 'linux-build.yml'),
             event='workflow_dispatch' if platform=='macos' else 'push',repository=repo,head_repository=repo)
    art=dict(id=456,name='macos-arm64-build-18-9' if platform=='macos' else 'meeting-summary-linux-9',expired=False,
             size_in_bytes=1000,digest='sha256:'+SHA,workflow_run=dict(id=123,repository_id=1442233273,
             head_repository_id=1442233273,head_branch='main',head_sha=HEAD))
    return repo,run,art

class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(delivery,'Missing separate build-preview helper')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.sequence=0
    def validate(self,platform,files=None,extra=None):
        path=self.root/'source.zip';sha=archive(path,files or fixtures(platform),extra)
        self.sequence+=1
        return delivery.validate_archive(path,sha,self.root/f'out-{self.sequence}',platform,HEAD)
    def test_valid_platform_payload_renames_and_explicitly_disclaims_acceptance(self):
        for platform,count in [('macos',2),('linux',3)]:
            with self.subTest(platform=platform):
                out=self.root/platform;out.mkdir();p=out/'input.zip';sha=archive(p,fixtures(platform))
                payload=delivery.validate_archive(p,sha,out/'verified',platform,HEAD)
                self.assertEqual(len(payload),count)
                self.assertTrue(all('beta18' in name for name in payload))
                checks=[path for name,path in payload.items() if name.startswith('SHA256SUMS')][0].read_text()
                self.assertIn('BUILD PREVIEW ONLY',checks)
                self.assertIn('model_validation=not_run',checks)
                self.assertIn('runtime_acceptance=not_established',checks)
                for name,path in payload.items():
                    if not name.startswith('SHA256SUMS'): self.assertIn(digest(path.read_bytes())+'  '+name,checks)
    def test_provenance_requires_platform_workflow_main_private_success_exact_ids(self):
        for platform in ('macos','linux'):
            data=provenance(platform);delivery.validate_provenance(*data,123,456,SHA,HEAD,platform)
            for index,key,value in [(0,'private',False),(0,'id',3),(1,'head_branch','guard'),(1,'event','pull_request'),
                (1,'conclusion','failure'),(1,'head_sha','c'*40),(1,'path','.github/workflows/macos-models.yml'),
                (2,'digest','sha256:'+'0'*64),(2,'expired',True),(2,'id',457)]:
                changed=copy.deepcopy(data);changed[index][key]=value
                with self.assertRaises(ValueError):delivery.validate_provenance(*changed,123,456,SHA,HEAD,platform)
    def test_artifact_origin_must_match_even_when_run_itself_is_approved(self):
        data=provenance('linux')
        for key,value in [('id',124),('head_sha','c'*40),('head_repository_id',3),('head_branch','guard')]:
            changed=copy.deepcopy(data);changed[2]['workflow_run'][key]=value
            with self.assertRaises(ValueError):delivery.validate_provenance(*changed,123,456,SHA,HEAD,'linux')

    def test_linux_rejects_any_model_request_pass_or_inference_evidence(self):
        for key,value in [('inference_requested',True),('release_eligible',True),('checks',{**CHECKS,'INFERENCE':'success'}),('checks',{**CHECKS,'BUILD':'failure'})]:
            files=fixtures('linux');gate=json.loads(files['linux-artifacts/ci-gates.json']);gate[key]=value;files['linux-artifacts/ci-gates.json']=encode(gate)
            with self.assertRaises(ValueError):self.validate('linux',files)
        files=fixtures('linux');files['linux-inference.json']=b'{}'
        with self.assertRaises(ValueError):self.validate('linux',files)
        files=fixtures('linux');files['linux-gui.json']=encode({'model_validation':'passed'})
        with self.assertRaises(ValueError):self.validate('linux',files)
    def test_mac_receipts_commit_build_workers_and_native_checks_must_agree(self):
        mutations=[('package-receipt.json','commit','c'*40),('package-receipt.json','build','17'),
          ('saved-archive-receipt.json','signature_verified',False),('saved-archive-receipt.json','sha256','0'*64),
          ('saved-archive-receipt.json','workers',{}),('runtime-factory-receipt.json','complete',False),
          ('package-receipt.json','model_validation','passed'),('package-receipt.json','audio_validation','passed'),
          ('package-receipt.json','worker_id','0'*20)]
        for file,key,value in mutations:
            files=fixtures('macos');body=json.loads(files[file]);body[key]=value;files[file]=encode(body)
            with self.assertRaises(ValueError):self.validate('macos',files)

    def test_mac_inner_build_and_worker_bytes_are_verified_beyond_outer_checksums(self):
        for changed_name,new_data in [('Info.plist',plistlib.dumps(dict(CFBundleVersion='17'))),('Resources/workers/worker.py',b'changed worker')]:
            files=fixtures('macos');old=zipfile.ZipFile(io.BytesIO(files[MAC]));target=io.BytesIO()
            with zipfile.ZipFile(target,'w') as inner:
                for info in old.infolist():
                    data=new_data if info.filename=='Сводка встречи.app/Contents/'+changed_name else old.read(info)
                    inner.writestr(info,data)
            old.close();files[MAC]=target.getvalue();sha=digest(files[MAC])
            files['SHA256SUMS']=(sha+'  '+MAC+'\n').encode()
            saved=json.loads(files['saved-archive-receipt.json']);saved.update(sha256=sha,bytes=len(files[MAC]));files['saved-archive-receipt.json']=encode(saved)
            with self.assertRaises(ValueError):self.validate('macos',files)


    def test_ditto_utf8_names_without_language_flag_and_compressed_dependency_data(self):
        import struct
        files=fixtures('macos');inner=io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(files[MAC])) as source, zipfile.ZipFile(inner,'w',zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():target.writestr(info,source.read(info))
            target.writestr('Сводка встречи.app/Contents/Resources/runtime/scipy/testdata/repetitive.dat',b'x'*32000)
        raw=bytearray(inner.getvalue())
        with zipfile.ZipFile(io.BytesIO(raw)) as source:
            entries=source.infolist();cursor=source.start_dir
            dependency=entries[-1]
            self.assertGreater(dependency.file_size/dependency.compress_size,200)
            self.assertLess(dependency.file_size/dependency.compress_size,1000)
            for info in entries:
                struct.pack_into('<H',raw,info.header_offset+6,info.flag_bits & ~0x800)
                struct.pack_into('<H',raw,cursor+8,info.flag_bits & ~0x800)
                name,extra,comment=struct.unpack_from('<HHH',raw,cursor+28)
                cursor+=46+name+extra+comment
        files[MAC]=bytes(raw);sha=digest(files[MAC]);files['SHA256SUMS']=(sha+'  '+MAC+'\n').encode()
        saved=json.loads(files['saved-archive-receipt.json']);saved.update(sha256=sha,bytes=len(files[MAC]));files['saved-archive-receipt.json']=encode(saved)
        payload=self.validate('macos',files)
        self.assertEqual(set(payload),{'MeetingSummary-0.7.0-beta18-macos-arm64.zip','SHA256SUMS-beta18-macos-arm64.txt'})

    def test_inner_declared_entry_and_total_expansion_bounds_reject_bombs(self):
        import struct
        for count,claimed_size in [(1,512*1024**2+1),(7,512*1024**2)]:
            files=fixtures('macos');inner=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(files[MAC])) as source, zipfile.ZipFile(inner,'w') as target:
                for info in source.infolist():target.writestr(info,source.read(info))
                for number in range(count):target.writestr(f'Сводка встречи.app/Contents/Resources/bomb{number}.dat',b'x')
            raw=bytearray(inner.getvalue())
            with zipfile.ZipFile(io.BytesIO(raw)) as source:
                cursor=source.start_dir
                for info in source.infolist():
                    if '/bomb' in info.filename:
                        # Declared ratio stays below1000; absolute limits must reject
                        # before reading or allocating the claimed dependency data.
                        struct.pack_into('<I',raw,cursor+20,1024**2)
                        struct.pack_into('<I',raw,cursor+24,claimed_size)
                    name,extra,comment=struct.unpack_from('<HHH',raw,cursor+28)
                    cursor+=46+name+extra+comment
            files[MAC]=bytes(raw);sha=digest(files[MAC]);files['SHA256SUMS']=(sha+'  '+MAC+'\n').encode()
            saved=json.loads(files['saved-archive-receipt.json']);saved.update(sha256=sha,bytes=len(files[MAC]));files['saved-archive-receipt.json']=encode(saved)
            with self.assertRaisesRegex(ValueError,'Inner ZIP expansion bound exceeded'):
                self.validate('macos',files)

    def test_wrapper_rejects_unknown_paths_duplicates_links_bombs_and_wrong_sha(self):
        link=zipfile.ZipInfo('linux-capture.log');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
        for extra in [('../escape',b'x'),('run.py',b'x'),('linux-capture.log',b'dup'),(link,b'target'),('linux-space.log',b'0'*3_000_000)]:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with self.assertRaises(ValueError):self.validate('linux',extra=extra)
        p=self.root/'wrong.zip';archive(p,fixtures('linux'))
        with self.assertRaises(ValueError):delivery.validate_archive(p,'0'*64,self.root/'wrong','linux',HEAD)
    def test_unique_title_private_draft_allows_untagged_but_never_other_or_published(self):
        draft=dict(id=77,name=TITLE,draft=True,tag_name='untagged-abc123')
        self.assertEqual(delivery.select_release([draft])['id'],77)
        for releases in ([],[draft,draft],[{**draft,'draft':False}],[{**draft,'name':'0.7.0 beta — build 17'}]):
            with self.assertRaises(ValueError):delivery.select_release(releases)
    def test_collisions_block_all_and_matching_assets_skip(self):
        payload=self.validate('linux')
        assets=[dict(name=name,size=p.stat().st_size,digest='sha256:'+digest(p.read_bytes()),state='uploaded') for name,p in payload.items()]
        self.assertEqual(delivery.upload_plan(payload,assets),{})
        assets[-1]['digest']='sha256:'+'0'*64
        with self.assertRaises(ValueError):delivery.upload_plan(payload,assets)


class PreviewOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
    def api_fixture(self,platform,changed_id=False):
        source=self.root/'source.zip';sha=archive(source,fixtures(platform));repo,run,artifact=provenance(platform)
        artifact.update(digest='sha256:'+sha,size_in_bytes=source.stat().st_size)
        release=dict(id=77,name=TITLE,draft=True,tag_name='untagged-xyz')
        other=dict(id=91,name='some-existing-other-platform.zip',size=15,digest='sha256:'+'0'*64,state='uploaded')
        class API:
            def __init__(self):self.assets=[copy.deepcopy(other)];self.uploaded=[];self.queries=0
            def get(self,path):
                routes={delivery.PREFIX:repo,delivery.PREFIX+'/actions/runs/123':run,
                    delivery.PREFIX+'/actions/artifacts/456':artifact,delivery.PREFIX+'/releases/77':release}
                return copy.deepcopy(routes[path])
            def pages(self,path):
                if path==delivery.PREFIX+'/releases':
                    self.queries+=1
                    return [{**release,'id':78 if changed_id and self.queries>1 else 77}]
                if path==delivery.PREFIX+'/releases/77/assets':return copy.deepcopy(self.assets)
                raise AssertionError('Unexpected API route')
            def download(self,artifact_id,target):
                if artifact_id!=456:raise AssertionError('Wrong artifact')
                target.write_bytes(source.read_bytes())
            def upload(self,release_id,name,path):
                if release_id!=77:raise AssertionError('Wrong stable release ID')
                self.assets.append(dict(name=name,size=path.stat().st_size,digest='sha256:'+digest(path.read_bytes()),state='uploaded'))
                self.uploaded.append(name)
        return API(),sha,other
    def test_end_to_end_preview_only_resume_keeps_unrelated_assets_and_draft(self):
        for platform,count in [('macos',2),('linux',3)]:
            with self.subTest(platform=platform):
                api,sha,other=self.api_fixture(platform)
                root=self.root/platform;root.mkdir()
                receipt=delivery.deliver(api,platform,123,456,sha,HEAD,root)
                self.assertEqual(len(api.uploaded),count)
                self.assertEqual(api.assets[0],other)
                self.assertIs(receipt['release_eligible'],False)
                self.assertEqual(receipt['model_validation'],'not_run')
                self.assertEqual(receipt['runtime_acceptance'],'not_established')
                root2=self.root/(platform+'-again');root2.mkdir()
                delivery.deliver(api,platform,123,456,sha,HEAD,root2)
                self.assertEqual(len(api.uploaded),count)
    def test_stable_id_or_asset_conflict_stops_before_first_upload(self):
        api,sha,_=self.api_fixture('linux',changed_id=True);folder=self.root/'changed';folder.mkdir()
        with self.assertRaises(ValueError):delivery.deliver(api,'linux',123,456,sha,HEAD,folder)
        self.assertEqual(api.uploaded,[])
        api,sha,_=self.api_fixture('linux');name='SHA256SUMS-beta18-linux-amd64.txt'
        api.assets.append(dict(name=name,size=3,state='uploaded',digest='sha256:'+'0'*64))
        folder=self.root/'conflict';folder.mkdir()
        with self.assertRaises(ValueError):delivery.deliver(api,'linux',123,456,sha,HEAD,folder)
        self.assertEqual(api.uploaded,[])
    def test_network_lists_paginate_and_signed_storage_never_receives_bearer(self):
        import urllib.error
        class Response(io.BytesIO):pass
        class Opener:
            def __init__(self):self.requests=[]
            def open(self,request,timeout):
                self.requests.append(request)
                if '/actions/artifacts/' in request.full_url:
                    raise urllib.error.HTTPError(request.full_url,302,'redirect',{'Location':'https://productionresultssa0.blob.core.windows.net/signed'},None)
                if '.blob.core.windows.net/' in request.full_url:return Response(b'zip')
                return Response(b'[]')
        api=delivery.GitHub('synthetic-token');api.opener=Opener()
        self.assertEqual(api.pages(delivery.PREFIX+'/releases'),[])
        api.download(456,self.root/'download')
        self.assertEqual(api.opener.requests[-2].get_header('Authorization'),'Bearer synthetic-token')
        self.assertIsNone(api.opener.requests[-1].get_header('Authorization'))
        for url in ('http://productionresultssa0.blob.core.windows.net/a','https://evil.example/a','https://api.github.com@evil.example/a'):
            with self.assertRaises(ValueError):delivery.download_location(url)

if __name__=='__main__':unittest.main()
