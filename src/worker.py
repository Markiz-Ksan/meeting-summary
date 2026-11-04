"""Offline, bounded speech-to-summary worker. JSON lines over private child pipes."""
import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from collections import deque
from recent_transcript import RecentTranscript
from transcript_log import TranscriptLog

ROOT = Path(__file__).resolve().parents[1]
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'


def emit(event, **kwargs):
    print(json.dumps({'event': event, **kwargs}, ensure_ascii=False), flush=True)


def trim_overlap(previous, current):
    old, new = previous.split(), current.split()
    normalize = lambda w: re.sub(r'[^\w]', '', w.lower())
    for count in range(min(12, len(old), len(new)), 0, -1):
        if [normalize(w) for w in old[-count:]] == [normalize(w) for w in new[:count]]:
            return ' '.join(new[count:])
    return current.strip()


class ArchiveWriteError(OSError):
    """The input cannot be ACKed until its transcript reaches persistent storage."""


def aligned_segments(result, window_start, window_seconds):
    """Use existing decoder token times, without another model or decode pass."""
    tokens = getattr(result, 'tokens', None)
    if not tokens:
        return None
    segments, pieces = [], []
    began = previous_end = None
    for token in tokens:
        start, duration = float(token.start), float(token.duration)
        if not math.isfinite(start) or not math.isfinite(duration) or start < 0 or start > window_seconds+0.5 or duration < 0:
            raise ValueError('Некорректная временная разметка распознавания')
        if pieces and start-previous_end >= 1:
            segments.append((window_start+began, ''.join(pieces).strip()))
            pieces = []
        if not pieces:
            began = min(start, window_seconds)
        pieces.append(token.text)
        previous_end = start+duration
        if re.search(r'[.!?;]\s*$', token.text):
            segments.append((window_start+began, ''.join(pieces).strip()))
            pieces = []
    if pieces:
        segments.append((window_start+began, ''.join(pieces).strip()))
    return [(stamp, text) for stamp, text in segments if any(c.isalnum() for c in text)]


class Engine:
    def __init__(self, speech=True, transcript_log=None, summarization=True):
        self.transcript_log = transcript_log
        import mlx.core as mx
        self.mx = mx
        mx.set_cache_limit(256 * 1024**2)
        self.model = self.tokenizer = self.sampler = None
        if summarization:
            from mlx_lm import load
            from mlx_lm.sample_utils import make_sampler
            self.sampler = make_sampler(temp=0)
            emit('loading', text='Загрузка модели смысловой сводки ' + os.environ.get('MEETING_SUMMARY_MODEL_LABEL', 'Qwen3.5 9B') + '…')
            with contextlib.redirect_stdout(sys.stderr):
                self.model, self.tokenizer = load(str(ROOT / 'runtime/models/summary-9b'))
        self.speech = None
        if speech:
            emit('loading', text='Загрузка распознавания русской речи…')
            from parakeet_mlx.utils import from_config
            import mlx.nn as nn
            speech_path = ROOT / 'runtime/models/speech-4bit'
            with contextlib.redirect_stdout(sys.stderr):
                config = json.loads((speech_path / 'config.json').read_text())
                self.speech = from_config(config)
                nn.quantize(self.speech, bits=config['quantization']['bits'], group_size=config['quantization']['group_size'])
                self.speech.load_weights(str(speech_path / 'model.safetensors'))
                mx.eval(self.speech.parameters())
        self.history = deque(maxlen=12)
        self.recent = RecentTranscript(seconds=300)
        if transcript_log is not None:
            self.recent.entries = list(transcript_log.history.entries[-300:])
        self.audio = None
        self.audio_timestamp = None
        self.tail_has_speech = False
        self.last_summary_at = float('-inf')
        self.last_summary_context = ''
        if speech:
            emit('loading', text='Прогрев распознавания · только при запуске…')
            from parakeet_mlx.audio import get_logmel
            with contextlib.redirect_stdout(sys.stderr):
                mel = get_logmel(mx.zeros(self.speech.preprocessor_config.sample_rate * 6), self.speech.preprocessor_config)
                self.speech.generate(mel)
            mx.clear_cache()

    def summarize(self, text):
        from summary_writer import write_summary
        # Condense the recent transcript, then review the draft against the same source.
        sentences = [s.strip() for s in re.split(r'(?<=[.!?;])\s+|\n+', text) if s.strip()]
        boilerplate = re.compile(r'^(доброе утро|добрый (день|вечер)|всем привет|привет всем|меня (хорошо )?слышно|всем (всё )?видно|давайте нач(инать|нём)|да[, ]+вс[её] (отлично|хорошо)|да[, ]+слышно|(?:тогда )?жд[её]м остальных)[.!? ]*$', re.I)
        sentences = [s for s in sentences if not boilerplate.match(s)]
        sentences = sentences[-80:]
        while len(sentences) > 1 and len(" ".join(sentences)) > 10000:
            sentences.pop(0)
        if not sentences:
            return ''
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return write_summary(self.model, self.tokenizer, ' '.join(sentences)[-10000:], self.sampler)
        finally:
            # Rejected drafts must release unused generation buffers as well.
            # The loaded model remains available for the next context.
            self.mx.clear_cache()

    def title(self, text):
        from session_title import generate_title
        from mlx_lm import generate
        def generator(messages):
            prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
            return generate(self.model, self.tokenizer, prompt=prompt, max_tokens=180, sampler=self.sampler, verbose=False)
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return generate_title(generator, text)
        finally:
            self.mx.clear_cache()

    def transcribe(self, path, timestamp):
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        from math import gcd
        from parakeet_mlx.audio import get_logmel
        self.transcript_segments = None
        audio, rate = sf.read(path, dtype='float32', always_2d=True)
        if rate <= 0 or len(audio) == 0 or len(audio) > rate*45:
            raise ValueError('Пустой или слишком длинный аудиофрагмент')
        audio = audio.mean(axis=1)
        if not np.isfinite(audio).all():
            raise ValueError('Некорректные аудиоданные')
        duration = len(audio)/rate
        target = self.speech.preprocessor_config.sample_rate
        if rate != target:
            divisor = gcd(rate, target)
            audio = resample_poly(audio, target//divisor, rate//divisor)
        audible = len(audio) > 0 and float(np.sqrt(np.mean(audio**2))) >= 0.0003
        self.tail_has_speech = len(audio) > 0 and float(np.sqrt(np.mean(audio[-int(target*0.25):]**2))) >= 0.0005
        if self.audio_timestamp is None or timestamp-self.audio_timestamp >= 45 or timestamp < self.audio_timestamp:
            self.audio = np.empty(0, dtype='float32')
            self.history.clear()
        else:
            gap = timestamp-duration-self.audio_timestamp
            if gap > 0.1:
                self.audio = np.concatenate((self.audio, np.zeros(min(int(gap*target), target*45), dtype='float32')))
            elif gap < -0.1:
                audio = audio[min(len(audio), int(-gap*target)):]
        self.audio = np.concatenate((self.audio, audio))[-target*45:]
        self.audio_timestamp = timestamp
        if not audible:
            return ''
        with contextlib.redirect_stdout(sys.stderr):
            mel = get_logmel(self.mx.array(self.audio), self.speech.preprocessor_config)
            result = self.speech.generate(mel)[0]
            text = result.text.strip()
            seconds = len(self.audio)/target
            self.transcript_segments = aligned_segments(result, timestamp-seconds, seconds)
        return text

    def process(self, path, timestamp, *, summarize=True, keep_audio=False, request_id=None):
        started = time.monotonic()
        if self.transcript_log is not None and self.transcript_log.was_committed(request_id, timestamp):
            context = self.recent.update('', timestamp, 0)
            return dict(transcript='', summary='', summary_context=context, context_timestamp=timestamp,
                        seconds=0, memory_mb=round(self.mx.get_active_memory()/1024**2), replayed=True,
                        context_seconds=300, context_sentences=len(self.recent.entries), audio_seconds=0)
        try:
            text = self.transcribe(path, timestamp)
        finally:
            if not keep_audio:
                path.unlink(missing_ok=True)
        audio_seconds = len(self.audio)/self.speech.preprocessor_config.sample_rate
        # Archive and rolling summary are independent consumers. A failed LLM
        # must never delay a transcript commit or discard the unfinished phrase.
        if self.transcript_log is not None:
            try:
                self.transcript_log.update(text, timestamp, audio_seconds, request_id=request_id,
                                           segments=getattr(self, 'transcript_segments', None))
            except OSError as error:
                raise ArchiveWriteError('Не удалось сохранить расшифровку: ' + str(error)[:250]) from error
        context = self.recent.update(text, timestamp, audio_seconds, segments=getattr(self, 'transcript_segments', None))
        if text:
            self.history.clear()
            self.history.append((timestamp, context))
        summary = ''
        if summarize and text and len(context.split()) >= 2:
            if timestamp-self.last_summary_at >= 12 and context != self.last_summary_context:
                summary = self.summarize(context)
                self.last_summary_at = timestamp
                self.last_summary_context = context
            else:
                emit('loading', text='Речь распознана · смысловая сводка обновляется раз в 12 секунд')
        return dict(transcript=('Распознано: '+text[-480:]) if text else '', summary=summary,
                    seconds=round(time.monotonic()-started, 3),
                    memory_mb=round(self.mx.get_active_memory()/1024**2),
                    audio_seconds=round(audio_seconds, 2), context_seconds=300, context_sentences=len(self.recent.entries),
                    summary_context=context, context_timestamp=timestamp)


def validate_request_path(request, session):
    path = Path(request['path']).resolve(strict=True)
    if path.parent != session or path.suffix != '.wav' or path.stat().st_size > 12_000_000:
        raise ValueError('Недопустимый аудиофрагмент')
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--session', type=Path, required=True)
    parser.add_argument('--transcript-file', type=Path)
    parser.add_argument('--audio-file', type=Path)
    parser.add_argument('--audio-start', type=float)
    parser.add_argument('--mode', choices=('combined', 'transcribe', 'summarize'), default='combined')
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    stage = args.mode
    try:
        session = args.session.resolve(strict=True)
        transcript_log = TranscriptLog(args.transcript_file, resume=args.resume,
                                       audio_path=args.audio_file, audio_started_at=args.audio_start) if args.transcript_file and stage != 'summarize' else None
        engine = Engine(speech=stage != 'summarize', transcript_log=transcript_log, summarization=stage != 'transcribe')
    except Exception as error:
        emit('error', stage=stage, request_id=None, fatal=True,
             text='Не удалось запустить обработку: ' + str(error)[:250])
        return 1
    emit('ready', stage=stage)
    for line in sys.stdin:
        path = None
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get('request_id')
            timestamp = float(request.get('timestamp', time.time()))
            if not math.isfinite(timestamp):
                raise ValueError('Некорректное время аудиофрагмента')
            emit('progress', stage=stage, request_id=request_id)
            if stage == 'summarize':
                if 'title_text' in request:
                    text = request['title_text']
                    if not isinstance(text, str) or len(text) > 200_000:
                        raise ValueError('Недопустимый текст названия')
                    emit('result', stage=stage, request_id=request_id, title=engine.title(text))
                    continue
                text = request.get('text', '')
                if not isinstance(text, str) or len(text) > 200_000:
                    raise ValueError('Недопустимый текст сводки')
                started = time.monotonic()
                result = dict(summary=engine.summarize(text), context_timestamp=timestamp,
                              seconds=round(time.monotonic()-started, 3),
                              memory_mb=round(engine.mx.get_active_memory()/1024**2))
            else:
                path = validate_request_path(request, session)
                # Native retains input until ACK; a crash/restart can retry it.
                result = engine.process(path, timestamp, summarize=stage == 'combined', keep_audio=stage == 'transcribe', request_id=request_id)
            emit('result', stage=stage, request_id=request_id, **result)
        except ValueError as error:
            if stage == 'summarize':
                # A rejected paraphrase must not restart the loaded model or
                # erase a valid overlay; the next context remains processable.
                emit('result', stage=stage, request_id=request_id, summary='',
                     context_timestamp=timestamp, validation_failed=True)
            else:
                emit('error', stage=stage, request_id=request_id, fatal=False,
                     retryable=False, text=str(error)[:250])
        except Exception as error:
            if stage == 'combined' and path is not None and path.parent == session:
                path.unlink(missing_ok=True)
            emit('error', stage='archive' if isinstance(error, ArchiveWriteError) else stage,
                 request_id=request_id, fatal=False, retryable=isinstance(error, (OSError, RuntimeError)),
                 text='Не удалось обработать фрагмент: ' + str(error)[:250])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
