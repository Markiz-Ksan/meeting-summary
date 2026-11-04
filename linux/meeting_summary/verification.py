"""Runs only on explicit CLI flags. All inference tests use the synthetic fixture."""
import hashlib
import time
from pathlib import Path


def gui_smoke():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from .gui import Overlay
    app = QApplication.instance() or QApplication([])
    window = Overlay()
    window.show()
    app.processEvents()
    assert window.testAttribute(Qt.WA_TranslucentBackground)
    assert window.windowFlags() & Qt.WindowStaysOnTopHint
    assert window.start_button.isEnabled()
    assert not window.stop_button.isEnabled()
    assert window.mode.count() == 3
    text = '\n'.join(f'Строка {i}' for i in range(100))
    window.update_summary(text, 10)
    app.processEvents()
    scroll = window.summary.verticalScrollBar()
    scroll.setValue(40)
    window.update_summary(text+'\nПоследняя строка', 11)
    assert scroll.value() == 40, 'Summary update moved manually scrolled text'
    window.update_summary('outdated', 9)
    assert window.summary.toPlainText() != 'outdated'
    report = dict(gui=True, transparent=True, on_top_flag=True, source_modes=3,
                  stale_summary_rejected=True, manual_scroll_preserved=True,
                  tray_available=window.tray is not None, platform=app.platformName())
    import os
    screenshot = os.environ.get('MEETING_GUI_SCREENSHOT')
    if screenshot:
        window.grab().save(screenshot)
    if window.tray:
        window.tray.hide()
    window.deleteLater()
    app.processEvents()
    return report


def inference_test(path):
    """Decode actual supplied synthetic WAV, ground summary, and save/reopen the pair."""
    import subprocess
    import tempfile
    import wave
    from .models import ensure_models, manifest
    from .pipeline import MeetingPipeline, ProcessEngine
    path = Path(path).resolve(strict=True)
    # Never infer an arbitrary user meeting in automation. This is the shipped synthetic fixture.
    expected = 'ab59fe1aa9025ebe14c6fae09df53601b3f7ec747f358b103471482915d1125a'
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError('Only the authorized tests/meeting-sample.wav fixture is accepted')
    started = time.monotonic()
    files = ensure_models()
    speech = ProcessEngine('speech', files)
    summary = ProcessEngine('summary', files)
    try:
        speech.prepare()
        summary.prepare()
    except BaseException:
        speech.close()
        summary.close()
        raise
    models_seconds = time.monotonic()-started
    try:
        from .native import executable, environment
        pcm = subprocess.check_output([executable('ffmpeg'),'-v','error','-i',str(path),'-ar','48000','-ac','1',
                                       '-f','s16le','-c:a','pcm_s16le','pipe:1'], env=environment())
        events = []
        class SyntheticCapture:
            def __init__(self, mode, consume, failed):
                self.consume = consume
            def start(self):
                # Feed several chunks and a non-window-aligned tail through real pipeline.
                for start in range(0, len(pcm), 48000):
                    self.consume(pcm[start:start+48000])
            def stop(self):
                pass
        with tempfile.TemporaryDirectory(prefix='meeting-synthetic-') as root:
            pipeline = MeetingPipeline(speech, summary, lambda kind, **data: events.append((kind,data)),
                                       root=Path(root), capture_factory=SyntheticCapture)
            pipeline.start('system')
            pipeline.stop()
            errors = [data for kind,data in events if kind=='error']
            assert not errors, errors
            summaries = [data['text'] for kind,data in events if kind=='summary']
            assert summaries and summaries[-1].strip(), 'Actual LLM produced no accepted grounded summary'
            archive = pipeline.archive
            text = archive.transcript_path.read_text(encoding='utf-8')
            utterances = [line for line in text.splitlines() if line.startswith('[')]
            assert utterances and any(any(c.isalpha() for c in line[11:]) for line in utterances), text
            assert 15 < len(pcm)/96000 < 19, 'Unexpected synthetic fixture duration'
            assert archive.audio_path.stem == archive.transcript_path.stem
            with wave.open(str(archive.audio_path)) as audio:
                saved = audio.readframes(audio.getnframes())
            assert saved == pcm, 'Saved WAV lost or duplicated source samples'
            return dict(synthetic=True, fixture_sha256=actual, cpu_only=True,
                        model_revisions={name:info['revision'] for name,info in manifest().items()},
                        model_load_seconds=round(models_seconds,3), total_seconds=round(time.monotonic()-started,3),
                        audio_seconds=len(pcm)/96000, paired_stem=archive.audio_path.stem,
                        saved_pcm_exact=True, transcript=text, summary=summaries[-1],
                        events=[kind for kind,data in events])
    finally:
        speech.close()
        summary.close()
