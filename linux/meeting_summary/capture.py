"""PulseAudio/PipeWire-Pulse capture. No global audio routing or mute changes."""
import json
from collections import deque
import os
import shutil
import signal
import subprocess
import threading
import time
from .native import executable, environment


def capture_command(mode, system_source, microphone_source, executable='ffmpeg'):
    if mode not in ('system', 'microphone', 'both'):
        raise ValueError('Unknown capture mode')
    if mode != 'system' and (not microphone_source or microphone_source.endswith('.monitor')):
        raise ValueError('Микрофон не найден: выбран источник системного звука')
    if mode != 'microphone' and not system_source.endswith('.monitor'):
        raise ValueError('Не найден монитор системного выхода')
    command = [executable, '-hide_banner', '-loglevel', 'error', '-nostdin']
    sources = ([system_source] if mode == 'system' else [microphone_source] if mode == 'microphone'
               else [system_source, microphone_source])
    for source in sources:
        command += ['-thread_queue_size', '1024', '-f', 'pulse', '-i', source]
    if mode == 'both':
        command += ['-filter_complex', '[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0:normalize=1,aresample=48000:async=1:first_pts=0[a]', '-map', '[a]']
    else:
        command += ['-af', 'aresample=48000:async=1:first_pts=0']
    return command + ['-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1']


def sources():
    def pactl(*args):
        return subprocess.check_output([executable('pactl'), *args], text=True, timeout=5, env=environment()).strip()
    sink = pactl('get-default-sink')
    mic = pactl('get-default-source')
    if mic.endswith('.monitor'):
        available = json.loads(pactl('-f', 'json', 'list', 'sources'))
        mic = next((x['name'] for x in available if not x['name'].endswith('.monitor')), '')
    return sink+'.monitor', mic


class PulseCapture:
    def __init__(self, mode, consume, failed):
        self.mode, self.consume, self.failed = mode, consume, failed
        self.process = None
        self.stopping = False
        self.reader = None
        self.error_reader = None
        self.diagnostics = deque(maxlen=8)

    def start(self):
        system, mic = sources()
        command = capture_command(self.mode, system, mic, executable=executable('ffmpeg'))
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True, env=environment())
        self.error_reader = threading.Thread(target=self._read_errors, name='capture-diagnostics', daemon=True)
        self.error_reader.start()
        self.reader = threading.Thread(target=self._read, name='capture', daemon=True)
        self.reader.start()

    def _read_errors(self):
        while block := self.process.stderr.read(1024):
            self.diagnostics.append(block)

    def _read(self):
        try:
            while block := self.process.stdout.read(48000):  # 0.5 s of PCM16 mono
                self.consume(block)
            code = self.process.wait()
            if not self.stopping:
                if self.error_reader:
                    self.error_reader.join(timeout=2)
                reason = b''.join(self.diagnostics).decode('utf-8', 'replace')
                self.failed(RuntimeError(f'Захват прерван ({code}): {reason[-500:]}'))
        except BaseException as error:
            self.failed(error)
            self.stopping = True
            if self.process.poll() is None:
                self.process.terminate()
        finally:
            self.process.stdout.close()
            if self.error_reader:
                self.error_reader.join(timeout=2)
            self.process.stderr.close()

    def stop(self):
        self.stopping = True
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=10)
            if self.reader.is_alive():
                raise RuntimeError('Capture thread failed to drain')
