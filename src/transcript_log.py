"""Human-readable session transcript; rolling ASR corrections replace the overlapping tail."""
from datetime import datetime, timedelta
import os
import json
import re
from pathlib import Path
import tempfile
from recent_transcript import RecentTranscript


class TranscriptLog:
    def __init__(self, path, resume=False, audio_path=None, audio_started_at=None):
        self.path = Path(path)
        self.history = RecentTranscript(seconds=float('inf'), max_entries=None)
        session_start = datetime.fromtimestamp(audio_started_at).astimezone() if audio_started_at is not None else datetime.now().astimezone()
        self.header = ('Расшифровка встречи\n'
                       f'Начало: {session_start.isoformat(timespec="seconds")}\n'
                       'Время в строках — начало фразы по оценке распознавателя. Возможны ошибки распознавания.\n\n')
        if audio_path is not None and audio_started_at is not None:
            anchor = datetime.fromtimestamp(audio_started_at).astimezone().isoformat(timespec='milliseconds')
            self.header += (f'Аудиозапись: {audio_path}\n'
                            f'Начало аудиозаписи (00:00:00): {anchor}\n'
                            'Отметки реплик приблизительные, по временной разметке звука распознавателем.\n\n')
        self.last_saved = self.header
        self.last_request = None
        if resume and self.path.exists():
            self._restore()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A session never overwrites an existing meeting file.
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(self.header)

    def _restore(self):
        """Recover committed text after a worker crash without replacing the file."""
        content = self.path.read_text(encoding='utf-8')
        start = re.search(r'^Начало: (.+)$', content, re.M)
        if not content.startswith('Расшифровка встречи\n') or start is None:
            raise ValueError('Неизвестный формат существующей расшифровки')
        audio_anchor = re.search(r'^Начало аудиозаписи \(00:00:00\): (.+)$', content, re.M)
        day = datetime.fromisoformat(audio_anchor.group(1) if audio_anchor else start.group(1)).date()
        rows = list(re.finditer(r'^\[(\d{2}:\d{2}:\d{2})\] (.*)$', content, re.M))
        self.header = content[:rows[0].start()] if rows else content
        metadata = re.search(r'^Последний сохранённый фрагмент: (.+?) · .+? · метка ([0-9.eE+\-]+)$', self.header, re.M)
        if metadata:
            self.last_request = {'request_id': json.loads(metadata.group(1)), 'timestamp': float(metadata.group(2))}
            self.header = self.header[:metadata.start()]
        previous = None
        for row in rows:
            clock = datetime.strptime(row.group(1), '%H:%M:%S').time()
            stamp = datetime.combine(day, clock)
            if previous is not None and (previous-stamp).total_seconds() > 43200:
                day += timedelta(days=1)
                stamp = datetime.combine(day, clock)
            previous = stamp
            self.history.entries.append((stamp.timestamp(), row.group(2)))
        self.last_saved = content

    def was_committed(self, request_id, timestamp):
        return (request_id is not None and self.last_request is not None
                and self.last_request.get('request_id') == request_id
                and self.last_request.get('timestamp') == timestamp)

    def update(self, text, timestamp, audio_seconds, request_id=None, segments=None):
        if not text.strip() and request_id is None:
            return
        self.history.update(text, timestamp, audio_seconds, segments=segments)
        request = {'request_id': request_id, 'timestamp': timestamp} if request_id is not None else self.last_request
        # The ACK checkpoint and transcript share one atomic replacement. A
        # worker killed after saving but before ACK can replay without another
        # differently contextualized ASR decode duplicating the committed tail.
        metadata = ''
        if request is not None:
            saved_at = datetime.fromtimestamp(request['timestamp']).astimezone().isoformat(timespec='microseconds')
            identifier = json.dumps(request['request_id'], ensure_ascii=False)
            metadata = f"Последний сохранённый фрагмент: {identifier} · {saved_at} · метка {request['timestamp']!r}\n\n"
        content = self.header + metadata + ''.join(
            f'[{datetime.fromtimestamp(stamp).strftime("%H:%M:%S")}] {sentence}\n'
            for stamp, sentence in self.history.entries
        )
        if content == self.last_saved:
            return
        temporary = None
        try:
            # Readers always see a complete file, even while an ASR revision is saved.
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.transcript-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.last_saved = content
            self.last_request = request
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
