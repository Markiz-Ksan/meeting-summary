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


def capture_command(mode, system_source, microphone_source, executable='ffmpeg', log_level='error'):
    if mode not in ('system', 'microphone', 'both'):
        raise ValueError('Unknown capture mode')
    if mode != 'system' and (not microphone_source or microphone_source.endswith('.monitor')):
        raise ValueError('Микрофон не найден: выбран источник системного звука')
    if mode != 'microphone' and not system_source.endswith('.monitor'):
        raise ValueError('Не найден монитор системного выхода')
    if log_level not in ('error', 'warning', 'info', 'verbose', 'debug'):
        raise ValueError('Unknown FFmpeg log level')
    command = [executable, '-hide_banner', '-loglevel', log_level, '-nostdin']
    sources = ([system_source] if mode == 'system' else [microphone_source] if mode == 'microphone'
               else [system_source, microphone_source])
    for source in sources:
        # Pulse declares PCM codec/rate/channels in its header. Stream-info
        # decoding would serially consume live packets before reader threads run.
        # Skip that analysis, retain all packets, then rebase their original PTS
        # below so the wall-clock epoch never becomes years of leading silence.
        command += ['-thread_queue_size', '1024', '-fragment_size', '9600',
                    '-nofind_stream_info', '-f', 'pulse', '-i', source]
    if mode == 'both':
        command += ['-filter_complex', '[0:a]asetpts=PTS-STARTPTS[s];[1:a]asetpts=PTS-STARTPTS[m];[s][m]amix=inputs=2:duration=longest:dropout_transition=0:normalize=1,aresample=48000:async=1:first_pts=0[a]', '-map', '[a]']
    else:
        command += ['-af', 'asetpts=PTS-STARTPTS,aresample=48000:async=1:first_pts=0']
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
    def __init__(self, mode, consume, failed, *, log_level='error'):
        self.mode, self.consume, self.failed = mode, consume, failed
        self.process = None
        self.stopping = False
        self.reader = None
        self.error_reader = None
        self.diagnostics = deque(maxlen=8)
        self.log_level = log_level
        self.command = None
        self.started_at = self.first_pcm_at = self.stopped_at = None
        self.pcm_bytes = 0
        self.forced_kill = False

    def start(self):
        system, mic = sources()
        command = capture_command(self.mode, system, mic, executable=executable('ffmpeg'), log_level=self.log_level)
        self.command = command
        self.started_at = time.monotonic()
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
                if self.first_pcm_at is None:
                    self.first_pcm_at = time.monotonic()
                self.pcm_bytes += len(block)
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
                self.forced_kill = True
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=10)
            if self.reader.is_alive():
                raise RuntimeError('Capture thread failed to drain')
        self.stopped_at = time.monotonic()

    def diagnostic_snapshot(self):
        elapsed = lambda stamp: None if stamp is None or self.started_at is None else round(stamp-self.started_at, 6)
        return dict(mode=self.mode, command=self.command, pcm_bytes=self.pcm_bytes,
                    first_pcm_seconds=elapsed(self.first_pcm_at), stop_seconds=elapsed(self.stopped_at),
                    returncode=self.process.poll() if self.process is not None else None,
                    forced_kill=self.forced_kill,
                    stderr_tail=b''.join(self.diagnostics).decode('utf-8', 'replace'))
