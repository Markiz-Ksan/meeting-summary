"""Synthetic child only: no models, audio devices, playback or network."""
import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--mode', required=True)
parser.add_argument('--session', type=Path, required=True)
parser.add_argument('--transcript-file')
parser.add_argument('--resume', action='store_true')
args = parser.parse_args()
config = json.loads((args.session / 'fixture.json').read_text())
attempt_file = args.session / 'attempt.txt'
attempt = int(attempt_file.read_text()) + 1 if attempt_file.exists() else 1
attempt_file.write_text(str(attempt))
with (args.session / 'pids.txt').open('a') as stream:
    stream.write(f'{os.getpid()}\n')


def emit(event, **fields):
    print(json.dumps(dict(event=event, stage=args.mode, **fields)), flush=True)


hang = attempt == 1 or config['scenario'] == 'permanent_hang'
if hang:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if config['scenario'] == 'startup_hang':
        while True:
            emit('loading')
            time.sleep(0.01)

emit('ready')
for line in sys.stdin:
    request = json.loads(line)
    request_id = request['request_id']
    with (args.session / 'received.txt').open('a') as stream:
        stream.write(f'{request_id}\n')
    emit('progress', request_id=request_id)
    if hang:
        while True:
            # Neither repeated start, wrong-ID progress, stale result nor noisy
            # loading output may reset the active request's watchdog.
            emit('progress', request_id=request_id)
            emit('progress', request_id=-1)
            emit('result', request_id=-1)
            emit('loading')
            time.sleep(0.01)
    emit('result', request_id=request_id, transcript='synthetic', summary='synthetic')
