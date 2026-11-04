"""Durable full recording with a recoverable, paired timestamped transcript."""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid
import wave
from . import shared
from transcript_log import TranscriptLog

RATE = 48000
MAX_PCM_BYTES = 2**32 - 4096  # stop before RIFF overflow; approximately 12 hours


def default_documents():
    # Respect xdg-user-dirs, including localized Documents directories.
    import subprocess
    try:
        value = subprocess.check_output(['xdg-user-dir', 'DOCUMENTS'], text=True, timeout=3).strip()
        if value and Path(value).is_absolute():
            return Path(value)
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.home() / 'Documents'


class SessionArchive:
    def __init__(self, root=None, *, started=None, session_id=None):
        self.started = time.time() if started is None else float(started)
        self.session_id = session_id or uuid.uuid4().hex[:8]
        if not re.fullmatch('[a-f0-9]{8}', self.session_id):
            raise ValueError('Invalid session ID')
        now = datetime.fromtimestamp(self.started).astimezone()
        self.directory = Path(root or default_documents() / 'Сводка встреч') / now.strftime('%Y-%m-%d')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prefix = now.strftime('%H-%M-%S')
        self._paths('Встреча')
        self.lock = threading.RLock()
        self.frames = 0
        self.closed = False
        self.stream = self.audio_path.open('xb')
        try:
            os.chmod(self.audio_path, 0o600)
            self.writer = wave.open(self.stream, 'wb')
            self.writer.setnchannels(1)
            self.writer.setsampwidth(2)
            self.writer.setframerate(RATE)
            self.writer.writeframes(b'')
            self.stream.flush()
            self.transcript = TranscriptLog(self.transcript_path, audio_path=self.audio_path,
                                            audio_started_at=self.started)
            self._manifest('recording')
        except BaseException:
            self.stream.close()
            # Keep already-created files for diagnosis and never replace another session.
            raise

    def _paths(self, title):
        stem = f'{self.prefix} — {title} — {self.session_id}'
        self.audio_path = self.directory / (stem + '.wav')
        self.transcript_path = self.directory / (stem + '.txt')
        self.manifest_path = self.directory / (stem + '.json')

    def _manifest(self, state):
        data = dict(schema=1, transcript=str(self.transcript_path), audio=str(self.audio_path),
                    started=self.started, audio_started=self.started, session_id=self.session_id,
                    state=state, sample_rate=RATE, frames=self.frames)
        temporary = self.manifest_path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.manifest_path)

    def append(self, pcm):
        if len(pcm) % 2:
            raise ValueError('Incomplete PCM16 frame')
        with self.lock:
            if self.closed:
                raise ValueError('Archive closed')
            if self.frames*2 + len(pcm) > MAX_PCM_BYTES:
                raise OSError('Достигнут предел WAV: остановите встречу и начните новую')
            self.writer.writeframes(pcm)
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.frames += len(pcm)//2

    def read(self, start, count):
        with self.lock:
            with wave.open(str(self.audio_path), 'rb') as reader:
                reader.setpos(start)
                return reader.readframes(count)

    def close(self, *, incomplete=False):
        with self.lock:
            if self.closed:
                return
            self.writer.close()
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self.stream.close()
            self.closed = True
            self._manifest('incomplete' if incomplete else 'saved')

    def mark_complete(self, *, incomplete=False):
        with self.lock:
            if not self.closed: raise RuntimeError('Recording must be closed first')
            self._manifest('incomplete' if incomplete else 'saved')

    def finalize_title(self, title):
        if not self.closed:
            raise RuntimeError('Finalize only after recording and ASR drain')
        title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', title).strip(' .—-')[:72].strip()
        if not title or title == 'Встреча':
            return
        old_audio, old_text, old_meta = self.audio_path, self.transcript_path, self.manifest_path
        self._paths(title)
        if any(p.exists() for p in (self.audio_path, self.transcript_path, self.manifest_path)):
            self.audio_path, self.transcript_path, self.manifest_path = old_audio, old_text, old_meta
            raise FileExistsError('Session title destination exists')
        # Rename without overwrite, preserving a recoverable old copy until both exist.
        try:
            os.link(old_audio, self.audio_path)
            content = old_text.read_text(encoding='utf-8').replace(str(old_audio), str(self.audio_path))
            with self.transcript_path.open('x', encoding='utf-8') as stream:
                os.chmod(self.transcript_path, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._manifest('saved')
        except BaseException:
            # Old pair remains complete if finalization is interrupted.
            self.audio_path, self.transcript_path, self.manifest_path = old_audio, old_text, old_meta
            raise
        old_audio.unlink()
        old_text.unlink()
        old_meta.unlink()
        self.transcript.path = self.transcript_path
