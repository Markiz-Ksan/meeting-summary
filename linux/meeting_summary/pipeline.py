"""Capture never waits for ASR/LLM: audio is written before queuing disk offsets."""
import queue
import os
import multiprocessing
import threading
import time
from .archive import SessionArchive, RATE
from .capture import PulseCapture
from . import shared
from recent_transcript import RecentTranscript


def _process_metrics(pid=None):
    import resource
    import sys
    from pathlib import Path
    usage = resource.getrusage(resource.RUSAGE_SELF)
    pid = pid or os.getpid()
    result = dict(pid=pid, cpu_seconds=round(usage.ru_utime+usage.ru_stime,3),
                  max_rss_kib=int(usage.ru_maxrss/(1024 if sys.platform == 'darwin' else 1)),cpu_count=os.cpu_count())
    if pid != os.getpid(): result.update(cpu_seconds=None,max_rss_kib=None)
    try:
        cpu = Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()
        result['cpu_seconds'] = round((int(cpu[11])+int(cpu[12]))/os.sysconf('SC_CLK_TCK'),3)
        for line in Path(f'/proc/{pid}/status').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('VmRSS','VmSwap'):
                result[key.lower()+'_kib'] = int(value.split()[0])
            elif key == 'VmHWM':
                result['max_rss_kib'] = int(value.split()[0])
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, _, value = line.partition(':')
            if key in ('MemAvailable','SwapFree'):
                result[key.lower()+'_kib'] = int(value.split()[0])
    except (OSError,ValueError,IndexError):
        pass
    return result


def _engine_process(connection, kind, files, factory, diagnostics=False):
    """Native libraries are loaded only in a disposable, dedicated child."""
    try:
        if factory is None:
            from .inference import SpeechEngine, SummaryEngine
            factory = SpeechEngine if kind == 'speech' else SummaryEngine
        if diagnostics:
            connection.send(('metric', None, dict(event='model_load_start', **_process_metrics())))
        engine = factory(files)
        if diagnostics:
            connection.send(('metric', None, dict(event='model_load_done', **_process_metrics())))
        connection.send(('ready', None, None))
        while True:
            request_id, method, args = connection.recv()
            connection.send(('progress', request_id, None))
            if diagnostics:
                def metric(event, **data):
                    connection.send(('metric',request_id,dict(event=event,**data,**_process_metrics())))
                engine.diagnostic = metric
                metric('method_start',method=method)
            try:
                result = None if method == '__ready__' else getattr(engine, method)(*args)
                if diagnostics: metric('method_done',method=method)
                connection.send(('result', request_id, result))
            except ValueError as error:
                if diagnostics: metric('method_rejected',method=method,error_type=type(error).__name__,detail=str(error)[:400])
                connection.send(('rejected', request_id, str(error)))
            except BaseException as error:
                if diagnostics: metric('backend_error',method=method,error_type=type(error).__name__,detail=str(error)[:400])
                connection.send(('error', request_id, str(error)))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as error:
        if diagnostics:
            try: connection.send(('metric',None,dict(event='startup_error',error_type=type(error).__name__,detail=str(error)[:400],**_process_metrics())))
            except (EOFError,OSError): pass
        try: connection.send(('error', None, str(error)))
        except (EOFError, OSError): pass
    finally:
        connection.close()


class ProcessEngine:
    """One model per spawned child; a hung native call cannot trap the UI.

    Audio stays in SessionArchive. The exact in-flight request is replayed after
    killing and joining its old child; replacement never overlaps old weights.
    """
    def __init__(self, kind, files, *, factory=None, startup_timeout=90,
                 request_timeout=90, recovery_timeout=120, max_restarts=3, diagnostic=None):
        self.kind, self.files, self.factory = kind, files, factory
        self.diagnostic = diagnostic
        self.startup_timeout, self.request_timeout = startup_timeout, request_timeout
        self.recovery_timeout, self.max_restarts = recovery_timeout, max_restarts
        self.process = self.connection = None
        self.ready = False
        self._messages = queue.Queue()
        self._reader = self._sender = None
        self._serial = threading.Lock()
        self._state = threading.Lock()
        self._cancel = threading.Event()
        self._sequence = 0
        self._terminal = None

    def _trace(self, event, **data):
        if self.diagnostic:
            self.diagnostic(dict(event=event,kind=self.kind,monotonic=time.monotonic(),**data))

    def _launch(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_engine_process,
                                  args=(child, self.kind, self.files, self.factory, self.diagnostic is not None), daemon=True)
        with self._state:
            if self._cancel.is_set():
                parent.close(); child.close()
                raise InterruptedError('Обработка остановлена')
            process.start()
            self._trace("launch",pid=process.pid)
            child.close()
            self.process, self.connection = process, parent
            self.ready = False
            self._messages = messages = queue.Queue()
            def read_messages():
                try:
                    while True: messages.put(parent.recv())
                except (EOFError, OSError, ValueError) as error:
                    messages.put(('disconnected', None, str(error)))
            self._reader = threading.Thread(target=read_messages, name='model-pipe', daemon=True)
            self._reader.start()
        return parent

    def _kill(self):
        with self._state:
            process, connection = self.process, self.connection
            reader, sender = self._reader, self._sender
            self.process = self.connection = None
            self._reader = self._sender = None
            self.ready = False
        if process is not None:
            self._trace("kill",pid=process.pid,exitcode=process.exitcode,cancelled=self._cancel.is_set())
            if process.is_alive(): process.kill()
            process.join(timeout=2)
            if process.is_alive():
                raise RuntimeError('Не удалось завершить обработчик')
            process.close()
        if connection is not None:
            connection.close()
        for thread in (reader, sender):
            if thread is not None: thread.join(timeout=2)

    def close(self):
        self._cancel.set()
        self._kill()

    def _receive(self, connection, deadline):
        messages = self._messages
        next_sample = time.monotonic()+5
        while not self._cancel.is_set():
            if self.diagnostic and time.monotonic() >= next_sample:
                with self._state:
                    pid = self.process.pid if self.process else None
                if pid: self._trace('resource_sample',request_id=self._sequence,**_process_metrics(pid))
                next_sample = time.monotonic()+5
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Обработчик не отвечает; исходная запись сохранена')
            try:
                result = messages.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if result[0] == 'disconnected': raise EOFError(result[2])
            return result
        raise InterruptedError('Обработка остановлена')

    def _send(self, connection, request, deadline):
        # A suspended child can stop draining stdin even after ready. Large PCM
        # writes and partial framed replies must never block the watchdog itself.
        completed, errors = threading.Event(), []
        def send():
            try: connection.send(request)
            except BaseException as error: errors.append(error)
            finally: completed.set()
        with self._state:
            if self._cancel.is_set(): raise InterruptedError('Обработка остановлена')
            self._sender = threading.Thread(target=send, name='model-request', daemon=True)
            self._sender.start()
        while not completed.wait(.05):
            if self._cancel.is_set(): raise InterruptedError('Обработка остановлена')
            if time.monotonic() >= deadline: raise TimeoutError('Канал обработчика завис')
        if errors: raise OSError(str(errors[0])) from errors[0]

    def _call(self, method, *args, timeout=None):
        with self._serial:
            if self._terminal is not None: raise TimeoutError(self._terminal)
            self._sequence += 1
            request_id = self._sequence
            # Unlike heartbeat/readiness, only a completed matching result ends
            # this fixed episode. Cold reload/replay share its remaining budget.
            deadline = None if timeout is None else time.monotonic()+timeout
            for attempt in range(self.max_restarts+1):
                last_progress = time.monotonic()
                try:
                    if self._cancel.is_set(): raise InterruptedError('Обработка остановлена')
                    connection = self.connection or self._launch()
                    if not self.ready:
                        startup_deadline = min(deadline or float('inf'), time.monotonic()+self.startup_timeout)
                        while True:
                            event, _, payload = self._receive(connection, startup_deadline)
                            if event != 'metric': break
                            self._trace(**payload)
                        if event != 'ready': raise RuntimeError(str(payload))
                        self.ready = True
                        self._trace("ready",pid=self.process.pid,request_id=request_id)
                    last_progress = time.monotonic()
                    request_deadline = min(deadline or float('inf'), last_progress+self.request_timeout)
                    self._send(connection, (request_id, method, args), request_deadline)
                    self._trace("request_sent",request_id=request_id,method=method,attempt=attempt,deadline=request_deadline)
                    accepted = False
                    while True:
                        event, identifier, payload = self._receive(connection, request_deadline)
                        if event == 'metric':
                            self._trace(request_id=identifier,**payload)
                            continue
                        if identifier != request_id: continue
                        if event == 'progress' and not accepted:
                            accepted = True
                            self._trace("accepted",request_id=request_id,method=method,attempt=attempt)
                            last_progress = time.monotonic()
                            request_deadline = min(deadline or float('inf'), last_progress+self.request_timeout)
                        elif event == 'result':
                            self._trace('result',request_id=request_id,method=method,attempt=attempt)
                            return payload
                        elif event == 'rejected': raise ValueError(payload)
                        elif event == 'error': raise RuntimeError(payload)
                except ValueError:
                    self._trace("rejected",request_id=request_id,method=method,attempt=attempt)
                    raise  # Reject a draft without reloading its healthy model.
                except InterruptedError:
                    self._trace("cancelled",request_id=request_id,method=method,attempt=attempt)
                    self._kill()
                    raise
                except (OSError, EOFError, RuntimeError, TimeoutError) as error:
                    if deadline is None: deadline = last_progress+self.recovery_timeout
                    self._trace("retry",request_id=request_id,method=method,attempt=attempt,error_type=type(error).__name__,remaining=deadline-time.monotonic())
                    self._kill()
                    if self._cancel.is_set(): raise InterruptedError('Обработка остановлена') from error
                    if attempt == self.max_restarts or time.monotonic() >= deadline:
                        self._terminal = 'Восстановление обработки не завершено; аудио сохранено'
                        raise TimeoutError(self._terminal) from error
            raise TimeoutError('Обработка не завершена')

    def transcribe(self, pcm, rate, started): return self._call('transcribe', pcm, rate, started)
    def summarize(self, source): return self._call('summarize', source)
    def title(self, source): return self._call('title', source, timeout=20)
    def prepare(self): return self._call('__ready__', timeout=self.startup_timeout)


class LatestContext:
    def __init__(self):
        self.condition = threading.Condition()
        self.pending = None
        self.closed = False

    def put(self, text, stamp):
        with self.condition:
            if not self.closed:
                self.pending = (text, stamp)
                self.condition.notify()

    def get(self):
        with self.condition:
            self.condition.wait_for(lambda: self.pending is not None or self.closed)
            result, self.pending = self.pending, None
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class MeetingPipeline:
    def __init__(self, speech, summary, event, root=None, capture_factory=PulseCapture, drain_timeout=120):
        self.speech, self.summary, self.event = speech, summary, event
        self.root, self.capture_factory = root, capture_factory
        self.drain_timeout = drain_timeout
        self.aborting = threading.Event()
        self._stop_lock = threading.Lock()
        self._capture_gate = threading.Lock()
        self.archive = None
        self.capture = None
        self.boundaries = queue.SimpleQueue()
        self.summaries = LatestContext()
        self.last_queued = 0
        self.last_summary_timestamp = float('-inf')
        self.recent = RecentTranscript(seconds=300)
        self.failure = None
        self.stopping = threading.Event()
        self.asr_thread = self.summary_thread = None

    def start(self, mode):
        try:
            with self._capture_gate:
                if self.stopping.is_set(): return
                self.archive = SessionArchive(self.root)
                self.capture = self.capture_factory(mode, self.consume, self._capture_failed)
                self.asr_thread = threading.Thread(target=self._asr, name='recognition', daemon=True)
                self.summary_thread = threading.Thread(target=self._summarize, name='summary', daemon=True)
                self.asr_thread.start()
                self.summary_thread.start()
                if self.stopping.is_set(): return
                self.capture.start()
                if not self.stopping.is_set():
                    self.event('recording', audio=str(self.archive.audio_path), transcript=str(self.archive.transcript_path))
        except BaseException:
            self.stop()
            raise

    def consume(self, pcm):
        # Disk commit precedes recognition; no bounded queue may discard captured audio.
        self.archive.append(pcm)
        frames = self.archive.frames
        if frames-self.last_queued >= RATE*12:
            self.boundaries.put(frames)
            self.last_queued = frames

    def _capture_failed(self, error):
        self.failure = error
        self.event('error', text='Ошибка захвата: '+str(error))
        # Do not join capture from its own callback. Complete archive in a control thread.
        threading.Thread(target=self.stop, name='capture-recovery', daemon=True).start()

    def _asr(self):
        while True:
            end = self.boundaries.get()
            if self.aborting.is_set(): break
            if end is None:
                break
            start = max(0, end-RATE*45)
            timestamp = self.archive.started + end/RATE
            try:
                pcm = self.archive.read(start, end-start)
                text, segments = self.speech.transcribe(pcm, RATE, self.archive.started + start/RATE)
                self.archive.transcript.update(text, timestamp, (end-start)/RATE,
                                               request_id=f'linux-{end}', segments=segments)
                context = self.recent.update(text, timestamp, (end-start)/RATE, segments=segments)
                self.event('transcript', text=text, timestamp=timestamp,
                           backlog_seconds=round((self.archive.frames-end)/RATE, 1))
                if text and context and timestamp-self.last_summary_timestamp >= 12:
                    self.summaries.put(context, timestamp)
                    self.last_summary_timestamp = timestamp
            except BaseException as error:
                self.failure = error
                self.event('error', text='Расшифровка не завершена; аудио сохранено: '+str(error))
                threading.Thread(target=self.stop, name='asr-recovery', daemon=True).start()
                break
        self.summaries.close()

    def _summarize(self):
        while item := self.summaries.get():
            text, timestamp = item
            try:
                summary = self.summary.summarize(text)
                if summary:
                    self.event('summary', text=summary, timestamp=timestamp)
            except ValueError as error:
                self.event('status', text='Сводка проверяется; предыдущая сохранена')
            except BaseException as error:
                self.event('error', text='Ошибка сводки: '+str(error))

    def stop(self):
        # A capture error and a user stop may race. Exactly one owns finalization.
        if not self._stop_lock.acquire(blocking=False): return
        if self.stopping.is_set():
            self._stop_lock.release(); return
        self.stopping.set()
        try:
            with self._capture_gate:
                if self.capture: self.capture.stop()
            if self.archive is None:
                self.event('startup_failed', text='Запуск остановлен')
                return
            if self.archive:
                # Close the full WAV immediately AFTER capture's final pipe drain,
                # before any native inference wait. An inference hang cannot leave
                # an unfinalized recording behind. TXT can still update separately.
                self.archive.close(incomplete=True)
                if self.archive.frames > self.last_queued:
                    self.boundaries.put(self.archive.frames)
                self.boundaries.put(None)
                deadline = time.monotonic()+self.drain_timeout
                self.event('drain', phase='start',timeout=self.drain_timeout)
                for worker in (self.asr_thread, self.summary_thread):
                    if worker and worker is not threading.current_thread():
                        worker.join(timeout=max(0, deadline-time.monotonic()))
                self.event('drain',phase='deadline',asr_alive=bool(self.asr_thread and self.asr_thread.is_alive()),summary_alive=bool(self.summary_thread and self.summary_thread.is_alive()))
                if self.asr_thread and self.asr_thread.is_alive():
                    self.failure = TimeoutError('Расшифровка не завершена; полное аудио сохранено')
                if self.summary_thread and self.summary_thread.is_alive():
                    self.failure = self.failure or TimeoutError('Сводка не завершена; полное аудио и расшифровка сохранены')
                    self.event('error', text='Сводка не завершена: истёк лимит завершения обработки; аудио сохранено')
                self.aborting.set()
                if self.failure is None and not (self.summary_thread and self.summary_thread.is_alive()):
                    try:
                        content = self.archive.transcript_path.read_text(encoding='utf-8')
                        utterances = '\n'.join(line for line in content.splitlines() if line.startswith('['))
                        title = self.summary.title(utterances)
                        if title: self.archive.finalize_title(title)
                    except BaseException as error:
                        self.event('status', text='Запись сохранена с нейтральным названием: '+str(error))
                for engine in (self.speech, self.summary):
                    if hasattr(engine, 'close'): engine.close()
                for worker in (self.asr_thread, self.summary_thread):
                    if worker and worker is not threading.current_thread(): worker.join(timeout=2)
                self.archive.mark_complete(incomplete=self.failure is not None)
                # All native children are gone before reporting saved or allowing
                # a new meeting; no old model copy survives in the GUI process.
                self.event('saved', audio=str(self.archive.audio_path),
                           transcript=str(self.archive.transcript_path), incomplete=self.failure is not None)
        except BaseException as error:
            self.failure = error
            # Keep a failed capture drain open: it may still deliver PCM. Do not
            # close its sink early or claim saved/completed while the reader lives.
            self.event('error', text='Сохранение не завершено: '+str(error))
        finally:
            self._stop_lock.release()
