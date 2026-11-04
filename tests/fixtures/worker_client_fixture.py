"""A deterministic JSON child for WorkerClient checks. No audio or models."""
import argparse
import json
import os
import signal
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--mode')
parser.add_argument('--session')
parser.add_argument('--transcript-file')
parser.add_argument('--audio-file')
parser.add_argument('--audio-start', type=float)
parser.add_argument('--resume', action='store_true')
args = parser.parse_args()


def emit(event, **values):
    print(json.dumps(dict(event=event, **values)), flush=True)


if args.resume:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
else:
    print('fixture unexpected output: PRIVATE_DO_NOT_EXPOSE', flush=True)
    print('x' * (2 * 1024 * 1024 + 100), flush=True)
    sys.stderr.write('PRIVATE_DO_NOT_EXPOSE' * 1000)
    sys.stderr.flush()

emit('ready', pid=os.getpid(), audio_file=args.audio_file, audio_start=args.audio_start)
for line in sys.stdin:
    request = json.loads(line)
    if request.get('op') == 'exit':
        sys.stdout.write(json.dumps(dict(event='final', id=request['id'])))
        sys.stdout.flush()
        sys.exit(7)
    if request.get('op') == 'close_stdin':
        os.close(0)
        emit('stdin_closed')
        time.sleep(30)
        break
    emit('result', id=request.get('id'), length=len(request.get('text', '')))

if args.resume:
    while True:
        time.sleep(1)
