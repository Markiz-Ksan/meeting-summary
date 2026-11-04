"""Synthetic-only ten-minute parallel ASR/summary and lost-ACK recovery test."""
import json,os,queue,statistics,subprocess,sys,tempfile,threading,time
from pathlib import Path
from math import gcd
import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
ROOT=Path(__file__).resolve().parents[1]
MARKERS=['абрикос','библиотека','велосипед','водопад','галактика','дельфин','жираф','карандаш','крокодил','лимонад','мандарин','носорог','осьминог','пароход','пианино','самолёт','самовар','телескоп','фламинго','шоколад']

def run():
 started=time.monotonic(); events=queue.Queue(); children=[]
 env=dict(os.environ,HF_HUB_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='4')
 try:
  with tempfile.TemporaryDirectory(prefix='meeting-split-stress-') as folder:
   folder=Path(folder);transcript=folder/'synthetic.txt';rate=16000;blocks=[]
   filler,sr=sf.read(ROOT/'tests/meeting-sample.wav',dtype='float32');d=gcd(rate,sr);filler=resample_poly(filler,rate//d,sr//d)
   for i,marker in enumerate(MARKERS):
    path=folder/f'voice-{i}.aiff';phrase=f'Раздел {i+1}. Кодовое слово {marker}. Повторяю, {marker}. Документ по этому вопросу подготовят завтра.'
    subprocess.run(['/usr/bin/say','-v','Milena','-r','185','-o',str(path),phrase],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    audio,sr=sf.read(path,dtype='float32');d=gcd(rate,sr);audio=resample_poly(audio,rate//d,sr//d)
    blocks.append(np.concatenate((audio,np.zeros(int(rate*.3)),np.tile(filler,3)))[:rate*30])
   audio=np.concatenate(blocks);base=time.time();chunks=[]
   for i,start in enumerate(range(0,len(audio),int(rate*5.3))):
    end=min(len(audio),start+6*rate);path=folder/f'chunk-{i}.wav';sf.write(path,audio[start:end],rate)
    chunks.append(dict(request_id=i+1,path=str(path),timestamp=base+end/rate))
   print(json.dumps(dict(stage='prepared',chunks=len(chunks),seconds=round(time.monotonic()-started,1))),flush=True)
   def start_child(mode,resume=False):
    args=[sys.executable,'-u',str(ROOT/'src/worker.py'),'--session',str(folder),'--mode',mode]
    if mode=='transcribe':args+=['--transcript-file',str(transcript)]
    if resume:args+=['--resume']
    p=subprocess.Popen(args,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,bufsize=1);children.append(p)
    def read():
     for line in p.stdout:
      try:events.put((mode,p,json.loads(line)))
      except json.JSONDecodeError:pass
     events.put((mode,p,dict(event='exit',code=p.poll())))
    def drain():
     for line in p.stderr:pass
    threading.Thread(target=read,daemon=True).start();threading.Thread(target=drain,daemon=True).start();return p
   def send(p,message):p.stdin.write(json.dumps(message,ensure_ascii=False)+'\n');p.stdin.flush()
   asr=start_child('transcribe');summary=start_child('summarize');index=0;summary_ready=False;summary_busy=False;pending=None;asr_done=False;replaying=False;saved_before=None
   times=[];memories=[];results=[];summary_results=[];report=dict(synthetic=True,audio_seconds=len(audio)/rate,markers_expected=len(MARKERS));started=time.monotonic();deadline=started+290
   def send_next():
    nonlocal asr_done
    if index<len(chunks):send(asr,chunks[index])
    else:asr_done=True
   while time.monotonic()<deadline:
    if pending is not None and summary_ready and not summary_busy:send(summary,pending);pending=None;summary_busy=True
    if asr_done and not summary_busy and pending is None:break
    try:mode,p,e=events.get(timeout=1)
    except queue.Empty:continue
    if (mode=='transcribe' and p is not asr) or (mode=='summarize' and p is not summary):continue
    if e['event'] in ('error','exit'):raise RuntimeError(f'{mode}: {e}')
    if e['event']=='ready':
     if mode=='transcribe':send(asr,chunks[index-1]) if replaying else send_next()
     else:summary_ready=True
    if e['event']!='result':continue
    if mode=='summarize':
     summary_busy=False;summary_results.append(e);print(json.dumps(dict(stage='summary',source_second=round(e['context_timestamp']-base,1),seconds=e['seconds'],nonempty=bool(e.get('summary')))),flush=True);continue
    if replaying:
     assert e.get('replayed'),e
     assert transcript.read_bytes()==saved_before,'ACK replay rewrote transcript'
     replaying=False;report['resume_ack_idempotent']=True;send_next();continue
    assert e['request_id']==chunks[index]['request_id']
    assert Path(chunks[index]['path']).exists(),'WAV deleted before native ACK'
    times.append(e['seconds']);memories.append(e['memory_mb']);results.append(e)
    if e.get('summary_context'):pending=dict(text=e['summary_context'],timestamp=e['context_timestamp'],request_id=e['request_id'])
    index+=1
    if index%20==0:print(json.dumps(dict(stage='asr',processed=index,elapsed=round(time.monotonic()-started,1),last_seconds=e['seconds'])),flush=True)
    if index==35:
     saved_before=transcript.read_bytes();replaying=True;asr.stdin.close();asr.wait(timeout=10);asr=start_child('transcribe',resume=True)
    else:send_next()
   assert asr_done,'ASR did not finish before deadline'
   assert summary_results and summary_results[-1]['context_timestamp']==chunks[-1]['timestamp'],'Final summary did not catch up'
   text=transcript.read_text().lower().replace('ё','е');missing=[m for m in MARKERS if m.replace('ё','е') not in text]
   report.update(chunks=len(chunks),asr_results=len(results),summary_results=len(summary_results),wall_seconds=round(time.monotonic()-started,2),asr_median_seconds=round(statistics.median(times),3),asr_max_seconds=max(times),asr_model_memory_mb=max(memories),summary_model_memory_mb=max(r['memory_mb'] for r in summary_results),markers_found=len(MARKERS)-len(missing),markers_missing=missing,final_summary_nonempty=bool(summary_results[-1]['summary']),archive_lines=len(text.splitlines()),tail_marker_preserved='шоколад' in text)
   print(json.dumps(report,ensure_ascii=False,indent=2),flush=True);(ROOT/'tests/integration-split-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
   assert not missing,f'Synthetic markers missing: {missing}'
   assert report['tail_marker_preserved']
 finally:
  for p in children:
   if p.poll() is None:
    try:p.stdin.close();p.wait(timeout=10)
    except Exception:p.kill();p.wait()
if __name__=='__main__':run()
