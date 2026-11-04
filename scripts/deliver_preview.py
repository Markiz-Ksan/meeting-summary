#!/usr/bin/env python3
"""Attach BUILD-ONLY previews to one existing private draft. No qualification.

Stdlib only. Never creates, publishes, edits or deletes releases/assets. Never
executes archive content, loads models, captures devices or changes visibility.
The separate strict Linux release-delivery helper is not used or modified.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO='Markiz-Ksan/meeting-summary'
REPO_ID=1442233273
PREFIX='/repos/'+REPO
TITLE='0.7.0 beta — сборка 18'
MAX_ZIP=1024**3
MAX_TOTAL=2*1024**3
MAX_COMPRESSION_RATIO=1000  # Dependency test data can legitimately exceed200:1.
MAC='MeetingSummary-0.7.0-beta-macos-arm64.zip'
DEB='meeting-summary_0.7.0_amd64.deb'
APP='MeetingSummary-0.7.0-x86_64.AppImage'
WORKERS={'worker.py','summary_writer.py','recent_transcript.py','session_title.py','transcript_log.py'}
SOFTWARE={'CAPTURE','REGRESSIONS','GUI','BUILD','INSTALL'}
RENAMES={'macos':{MAC:'MeetingSummary-0.7.0-beta18-macos-arm64.zip'},'linux':{
    'linux-artifacts/'+DEB:'MeetingSummary-0.7.0-beta18-linux-amd64.deb',
    'linux-artifacts/'+APP:'MeetingSummary-0.7.0-beta18-linux-x86_64.AppImage'}}
CHECKSUMS={'macos':'SHA256SUMS-beta18-macos-arm64.txt','linux':'SHA256SUMS-beta18-linux-amd64.txt'}
MAC_JSON={'runtime-factory-receipt.json','package-receipt.json','saved-archive-receipt.json'}
LINUX_JSON={'linux-artifacts/'+n for n in ('ci-gates.json','frozen-gui.json','installed-gui.json','appimage-gui.json')}|{'linux-capture.json','linux-gui.json'}
IGNORED={'linux-capture.log','linux-space.log','linux-gui.png'}


def require(condition,message):
    if not condition: raise ValueError(message)


def sha256(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def load_json(data,object_required=True):
    def unique(pairs):
        output={}
        for key,value in pairs:
            require(key not in output,'Duplicate JSON key');output[key]=value
        return output
    try:
        result=json.loads(data.decode('utf-8'),object_pairs_hook=unique)
    except (UnicodeError,json.JSONDecodeError) as error:raise ValueError('Invalid UTF-8 JSON') from error
    require(not object_required or isinstance(result,dict),'Expected JSON object')
    return result


def validate_provenance(repo,run,artifact,run_id,artifact_id,expected_sha,head,platform):
    require(platform in RENAMES,'Invalid platform')
    require(re.fullmatch('[0-9a-f]{64}',expected_sha) and re.fullmatch('[0-9a-f]{40}',head),'Invalid SHA or commit')
    require(type(run_id) is int and run_id>0 and type(artifact_id) is int and artifact_id>0,'Invalid IDs')
    require(repo.get('id')==REPO_ID and repo.get('full_name')==REPO and repo.get('private') is True,'Approved repository must remain private')
    for key in ('repository','head_repository'):
        require(run.get(key,{}).get('id')==REPO_ID and run[key].get('full_name')==REPO,'Run repository mismatch')
    require(run.get('id')==run_id and run.get('head_sha')==head and run.get('head_branch')=='main','Wrong source main run/commit')
    require(run.get('status')=='completed' and run.get('conclusion')=='success','Source build must succeed')
    workflow='macos-package.yml' if platform=='macos' else 'linux-build.yml'
    require(run.get('path')=='.github/workflows/'+workflow,'Wrong platform build workflow')
    require(run.get('event') in ({'workflow_dispatch'} if platform=='macos' else {'push','workflow_dispatch'}),'Unapproved build event')
    name=('macos-arm64-build-18-' if platform=='macos' else 'meeting-summary-linux-')+str(run.get('run_number'))
    require(artifact.get('id')==artifact_id and artifact.get('name')==name and artifact.get('expired') is False,'Wrong/expired platform artifact')
    require(artifact.get('digest')=='sha256:'+expected_sha and 0<artifact.get('size_in_bytes',0)<=MAX_ZIP,'Artifact SHA or size mismatch')
    origin=artifact.get('workflow_run',{})
    require(all(origin.get(k)==v for k,v in dict(id=run_id,repository_id=REPO_ID,head_repository_id=REPO_ID,head_sha=head,head_branch='main').items()),'Artifact source mismatch')


def checksums(data,names):
    found={}
    try:lines=data.decode('ascii').splitlines()
    except UnicodeError as error:raise ValueError('Invalid checksums') from error
    for line in lines:
        match=re.fullmatch(r'([0-9a-f]{64})  (?:\./)?([^/\\]+)',line)
        require(match and match[2] in names and match[2] not in found,'Invalid checksum whitelist')
        found[match[2]]=match[1]
    require(set(found)==set(names),'Missing complete checksum set')
    return found


def smoke_checked(receipt):
    smoke=receipt.get('import_smoke',{})
    require(isinstance(smoke,dict) and str(smoke.get('python','')).startswith('3.12.13') and smoke.get('architecture')=='arm64'
            and smoke.get('models_loaded') is False and smoke.get('external_site_packages')==[],'Import-only software smoke mismatch')


def validate_mac_receipts(metadata,head):
    for receipt in metadata.values():
        require(receipt.get('commit')==head and receipt.get('build')=='18','Receipt source/build mismatch')
        require(receipt.get('model_validation')=='not_run' and receipt.get('audio_validation')=='not_run'
                and receipt.get('semantic_acceptance')=='not_evaluated','Preview must not claim model/audio/semantic acceptance')
        require(receipt.get('notarized') is False and receipt.get('signing')=='adhoc','Unexpected signing/notarization claims')
        smoke_checked(receipt)
    factory,package,saved=(metadata[n] for n in ('runtime-factory-receipt.json','package-receipt.json','saved-archive-receipt.json'))
    require(factory.get('complete') is True and factory.get('native_dependencies') and factory.get('retained_license_files'),'Factory software receipts incomplete')
    require(package.get('version')=='0.7.0' and package.get('worker_help') is True,'Package software verification incomplete')
    require(saved.get('signature_verified') is True and saved.get('minimum_macos')=='14.4' and saved.get('native_dependencies')
            and saved.get('network_during_import_check')=='denied','Saved archive verification incomplete')
    workers=package.get('workers',{})
    require(isinstance(workers,dict) and set(workers)==WORKERS and all(re.fullmatch('[0-9a-f]{64}',v) for v in workers.values()),'Unapproved worker set')
    require(saved.get('workers')==workers,'Worker receipts disagree')
    worker_id=hashlib.sha256(json.dumps(workers,sort_keys=True).encode()).hexdigest()[:20]
    require(package.get('worker_id')==worker_id,'Worker identity mismatch')
    return workers,worker_id


def verify_mac_zip(path,workers,worker_id):
    # Inspect targeted metadata and worker bytes; never extract or run the app.
    with zipfile.ZipFile(path,metadata_encoding='utf-8') as inner:
        entries=inner.infolist();require(0<len(entries)<=20000,'Inner app ZIP entry bound exceeded')
        seen=set();total=0
        for entry in entries:
            name=entry.filename;parts=PurePosixPath(name).parts
            require(name==entry.orig_filename and not name.startswith('/') and '\\' not in name and '..' not in parts and not any(ord(c)<32 for c in name),'Unsafe inner app ZIP name')
            require(name.casefold() not in seen,'Duplicate inner app ZIP name');seen.add(name.casefold())
            require(not entry.flag_bits&1 and entry.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED),'Unsupported inner ZIP encoding')
            total+=entry.file_size
            require(entry.file_size<=512*1024**2 and total<=3*1024**3 and entry.file_size<=max(1,entry.compress_size)*MAX_COMPRESSION_RATIO,'Inner ZIP expansion bound exceeded')
        base='Сводка встречи.app/Contents/'
        def read(name):
            entry=inner.getinfo(base+name)
            require(entry.file_size<=2*1024**2 and stat.S_IFMT(entry.external_attr>>16) in (0,stat.S_IFREG),'Invalid inspected app metadata')
            return inner.read(entry)
        try:info=plistlib.loads(read('Info.plist'))
        except Exception as error:raise ValueError('Invalid application plist') from error
        require(info.get('CFBundleVersion')=='18' and info.get('CFBundleShortVersionString')=='0.7.0' and info.get('LSMinimumSystemVersion')=='14.4','Inner app build/version mismatch')
        manifest=load_json(read('Resources/bundle-manifest.json'))
        require(manifest.get('workers')==workers and manifest.get('worker_id')==worker_id and manifest.get('models_bundled') is False,'Inner worker/model manifest mismatch')
        for name,expected in workers.items():
            require(hashlib.sha256(read('Resources/workers/'+name)).hexdigest()==expected,'Inner worker bytes mismatch')


def no_model_acceptance_claims(value):
    if isinstance(value,dict):
        for key,item in value.items():
            if key in ('model_validation','audio_validation'):
                require(item=='not_run','Preview cannot claim model/audio acceptance')
            if key=='semantic_acceptance':require(item=='not_evaluated','Preview semantic acceptance was not evaluated')
            if key in ('models_loaded','inference_requested','release_eligible'):
                require(item is False,'Model execution or release eligibility claim in software preview')
            require(key not in ('model_revisions','model_load_seconds','saved_pcm_exact','completion_tokens'),'Inference evidence forbidden in build-only artifact')
            no_model_acceptance_claims(item)
    elif isinstance(value,list):
        for item in value:no_model_acceptance_claims(item)


def validate_archive(path,expected_sha,output,platform,head):
    require(platform in RENAMES and re.fullmatch('[0-9a-f]{40}',head),'Invalid preview platform/head')
    path,output=Path(path),Path(output)
    require(path.stat().st_size<=MAX_ZIP and sha256(path)==expected_sha,'Wrapper ZIP SHA mismatch')
    source_sums='SHA256SUMS' if platform=='macos' else 'linux-artifacts/SHA256SUMS'
    json_names=MAC_JSON if platform=='macos' else LINUX_JSON
    allowed=set(RENAMES[platform])|{source_sums}|json_names|(IGNORED if platform=='linux' else set())
    required=set(RENAMES[platform])|{source_sums}|(MAC_JSON if platform=='macos' else {'linux-artifacts/ci-gates.json'})
    try:
        with zipfile.ZipFile(path) as wrapper:
            entries=wrapper.infolist();require(0<len(entries)<=32,'Wrapper entry bound exceeded')
            names=set();folded=set();total=0
            for entry in entries:
                name=entry.filename
                require(name in allowed and name==entry.orig_filename and name.isascii(),'Unapproved wrapper entry')
                require(name.casefold() not in folded,'Duplicate wrapper entry');folded.add(name.casefold());names.add(name)
                require(stat.S_IFMT(entry.external_attr>>16) in (0,stat.S_IFREG) and not entry.is_dir(),'Wrapper link/special file forbidden')
                require(not entry.flag_bits&1 and entry.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED),'Unsupported wrapper encoding')
                limit=MAX_ZIP if name in RENAMES[platform] else 2*1024**2 if name in json_names else 16*1024**2
                total+=entry.file_size
                require(entry.file_size<=limit and total<=MAX_TOTAL and entry.file_size<=max(1,entry.compress_size)*MAX_COMPRESSION_RATIO,'Wrapper expansion bound exceeded')
            require(required<=names,'Missing binaries/checksums/receipts')
            metadata={name:load_json(wrapper.read(name)) for name in json_names&names}
            if platform=='linux':
                no_model_acceptance_claims(metadata)
                gate=metadata['linux-artifacts/ci-gates.json'];checks=gate.get('checks',{})
                require(gate.get('inference_requested') is False and gate.get('release_eligible') is False and isinstance(checks,dict)
                        and set(checks)==SOFTWARE and all(v=='success' for v in checks.values()),'Linux preview requires only successful software gates, model checks not requested')
            else:workers,worker_id=validate_mac_receipts(metadata,head)
            expected=checksums(wrapper.read(source_sums),{Path(name).name for name in RENAMES[platform]})
            output.mkdir(parents=True,exist_ok=False);payload={}
            for source,name in RENAMES[platform].items():
                target=output/name
                with wrapper.open(source) as stream,target.open('xb') as sink:
                    remaining=wrapper.getinfo(source).file_size
                    while remaining:
                        chunk=stream.read(min(1024*1024,remaining));require(chunk,'Truncated wrapper member');sink.write(chunk);remaining-=len(chunk)
                    require(not stream.read(1),'Oversized wrapper member')
                require(sha256(target)==expected[Path(source).name],'Original binary SHA mismatch')
                payload[name]=target
            if platform=='macos':
                target=payload[RENAMES['macos'][MAC]];saved=metadata['saved-archive-receipt.json']
                require(saved.get('artifact')==MAC and saved.get('sha256')==sha256(target) and saved.get('bytes')==target.stat().st_size,'Saved archive receipt content mismatch')
                verify_mac_zip(target,workers,worker_id)
            sums=output/CHECKSUMS[platform]
            text=('# BUILD PREVIEW ONLY — NOT RELEASE QUALIFICATION\n# model_validation=not_run; audio_validation=not_run\n'
                  '# runtime_acceptance=not_established; semantic_acceptance=not_evaluated; release_eligible=false\n'
                  f'# source_commit={head}; build=18; platform={platform}\n')
            text+=''.join(sha256(p)+'  '+name+'\n' for name,p in payload.items())
            sums.write_text(text,encoding='utf-8');payload[sums.name]=sums
            return payload
    except (zipfile.BadZipFile,UnicodeError,RuntimeError,EOFError,KeyError) as error:raise ValueError('Invalid preview ZIP') from error


def select_release(releases):
    matches=[release for release in releases if release.get('name')==TITLE]
    require(len(matches)==1,'Expected exactly one existing build18 preview title')
    release=matches[0]
    require(release.get('draft') is True and type(release.get('id')) is int and release['id']>0,'Preview target must remain an existing draft')
    return release


def upload_plan(payload,assets):
    permitted=set(CHECKSUMS.values())|{name for platform in RENAMES.values() for name in platform.values()}
    plan={}
    for name,path in payload.items():
        require(name in permitted,'Only preview binaries and platform checksums may upload')
        matches=[asset for asset in assets if asset.get('name')==name]
        require(len(matches)<=1,'Duplicate preview asset')
        if matches:
            existing=matches[0]
            require(existing.get('state')=='uploaded' and existing.get('size')==path.stat().st_size and existing.get('digest')=='sha256:'+sha256(path),'Different existing preview asset; replacement forbidden')
        else:plan[name]=path
    return plan


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_location(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ''
    require(parsed.scheme == 'https' and not parsed.username and not parsed.password and parsed.port in (None,443) and re.fullmatch(r'productionresults[a-z0-9-]*\.blob\.core\.windows\.net',host), 'Untrusted signed artifact location')
    return url


class GitHub:
    def __init__(self, token):
        require(bool(token), 'GITHUB_TOKEN is required')
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, data=None, headers=None, uploads=False):
        require(path.startswith(PREFIX+'/') or path == PREFIX, 'API repository escape')
        url = ('https://uploads.github.com' if uploads else 'https://api.github.com')+path
        combined = {'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github+json',
                    'X-GitHub-Api-Version':'2022-11-28','User-Agent':'meeting-summary-draft-delivery'}
        combined.update(headers or {})
        return self.opener.open(urllib.request.Request(url,data=data,headers=combined), timeout=180)

    def get(self, path):
        with self.request(path) as response:
            data = response.read(8*1024*1024+1)
            require(len(data) <= 8*1024*1024, 'API JSON too large')
            return load_json(data,object_required=False)

    def pages(self, path):
        result = []
        for page in range(1,11):
            batch = self.get(path+f'?per_page=100&page={page}')
            require(isinstance(batch,list), 'Expected paginated list')
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise ValueError('API pagination exceeds bound')

    def download(self, artifact_id, destination):
        try:
            self.request(PREFIX+f'/actions/artifacts/{artifact_id}/zip')
        except urllib.error.HTTPError as error:
            require(error.code == 302, 'Expected signed artifact redirect')
            url = download_location(error.headers['Location'])
        else:
            raise ValueError('Expected signed artifact redirect')
        # No bearer header on signed storage requests, including further redirects.
        with self.opener.open(urllib.request.Request(url,headers={'User-Agent':'meeting-summary-draft-delivery'}),timeout=180) as source, Path(destination).open('xb') as target:
            total = 0
            while chunk := source.read(1024*1024):
                total += len(chunk)
                require(total <= MAX_ZIP, 'Downloaded artifact exceeds bound')
                target.write(chunk)

    def upload(self, release_id, name, path):
        with path.open('rb') as stream:
            with self.request(PREFIX+f'/releases/{release_id}/assets?name='+urllib.parse.quote(name,safe=''),
                              data=stream, uploads=True, headers={'Content-Type':'application/octet-stream','Content-Length':str(path.stat().st_size)}) as response:
                result = load_json(response.read(1024*1024))
        require(result.get('name') == name and result.get('state') == 'uploaded' and result.get('size') == path.stat().st_size and result.get('digest') == 'sha256:'+sha256(path), 'Uploaded asset verification failed')
        return result



def check_target(api,release_id):
    repo=api.get(PREFIX)
    require(repo.get('private') is True and repo.get('id')==REPO_ID and repo.get('full_name')==REPO,'Preview repository changed or became public')
    unique=select_release(api.pages(PREFIX+'/releases'))
    current=select_release([api.get(PREFIX+f'/releases/{release_id}')])
    require(unique['id']==release_id and current['id']==release_id,'Existing draft identity changed')


def deliver(api,platform,run_id,artifact_id,expected_sha,head,root):
    repo=api.get(PREFIX)
    run=api.get(PREFIX+f'/actions/runs/{run_id}')
    artifact=api.get(PREFIX+f'/actions/artifacts/{artifact_id}')
    validate_provenance(repo,run,artifact,run_id,artifact_id,expected_sha,head,platform)
    release=select_release(api.pages(PREFIX+'/releases'))
    release_id=release['id']
    source=Path(root)/'wrapper.zip'
    api.download(artifact_id,source)
    require(source.stat().st_size==artifact['size_in_bytes'],'Artifact download size mismatch')
    payload=validate_archive(source,expected_sha,Path(root)/'verified',platform,head)
    check_target(api,release_id)
    assets=api.pages(PREFIX+f'/releases/{release_id}/assets')
    plan=upload_plan(payload,assets)  # All collisions checked before the first write.
    for name,path in plan.items():
        check_target(api,release_id)
        api.upload(release_id,name,path)
    check_target(api,release_id)
    require(not upload_plan(payload,api.pages(PREFIX+f'/releases/{release_id}/assets')),'Final preview asset verification failed')
    return dict(preview=True,build='18',draft=True,release_id=release_id,platform=platform,
                release_eligible=False,model_validation='not_run',audio_validation='not_run',
                runtime_acceptance='not_established',semantic_acceptance='not_evaluated',
                source_run_id=run_id,artifact_id=artifact_id,wrapper_sha256=expected_sha,source_commit=head,
                assets={name:sha256(path) for name,path in payload.items()})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--platform',choices=('macos','linux'),required=True)
    parser.add_argument('--run-id',type=int,required=True)
    parser.add_argument('--artifact-id',type=int,required=True)
    parser.add_argument('--sha256',required=True)
    parser.add_argument('--head',required=True)
    args=parser.parse_args()
    require(os.environ.get('GITHUB_REPOSITORY')==REPO and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch'
            and os.environ.get('GITHUB_REF')=='refs/heads/main','Preview delivery requires manual execution on approved main')
    with tempfile.TemporaryDirectory(prefix='meeting-build-preview-') as root:
        receipt=deliver(GitHub(os.environ.get('GITHUB_TOKEN')),args.platform,args.run_id,args.artifact_id,args.sha256,args.head,root)
    print(json.dumps(receipt,ensure_ascii=False,indent=2))

if __name__=='__main__':
    try:main()
    except urllib.error.HTTPError as error:
        raise SystemExit(f'GitHub/storage request failed ({error.code}); release was never published') from None
    except urllib.error.URLError:
        raise SystemExit('GitHub/storage network request failed; signed URLs and credentials omitted') from None
