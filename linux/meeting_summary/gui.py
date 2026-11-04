"""Transparent, draggable overlay with tray and persistent visible controls."""
import os
from pathlib import Path
import threading
from PySide6.QtCore import QObject, QLockFile, QSettings, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QCloseEvent, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel, QMenu,
                               QPushButton, QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget)
from .pipeline import MeetingPipeline, ProcessEngine


class Events(QObject):
    received = Signal(str, dict)


class DragBar(QLabel):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()
        super().mousePressEvent(event)


class Overlay(QWidget):
    def __init__(self, controller=None):
        super().__init__()
        self.controller = controller
        self.latest_timestamp = float('-inf')
        self.setWindowTitle('Сводка встречи')
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(560, 350)
        self.setMinimumSize(390, 240)
        self.setStyleSheet('''QWidget#panel {background: rgba(24, 27, 34, 205); border: 1px solid rgba(255,255,255,65); border-radius: 14px;}
            QLabel,QTextEdit {color: #f1f3f8; background: transparent; border: none;}
            QTextEdit {font-size: 17px; selection-background-color: #466796;}
            QPushButton,QComboBox {background: #354150; color: #ffffff; padding: 7px; border-radius: 5px;}
            QPushButton:disabled {color: #89939f;}
            QComboBox QAbstractItemView {background: #354150; color: #ffffff;}''')
        wrapper = QVBoxLayout(self)
        wrapper.setContentsMargins(0, 0, 0, 0)
        panel = QWidget()
        panel.setObjectName('panel')
        wrapper.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 14)
        bar = QHBoxLayout()
        self.title = DragBar('Сводка встречи')
        self.title.setStyleSheet('font-size: 15px; font-weight: 600;')
        bar.addWidget(self.title, 1)
        self.hide_button = QPushButton('Скрыть')
        self.hide_button.clicked.connect(self.hide)
        bar.addWidget(self.hide_button)
        layout.addLayout(bar)
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setPlaceholderText('После начала записи здесь появится краткая сводка разговора.')
        self.summary.setAcceptRichText(False)
        layout.addWidget(self.summary, 1)
        self.status = QLabel('Готово к записи')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        for label, value in [('Система + микрофон', 'both'), ('Системный звук', 'system'), ('Микрофон', 'microphone')]:
            self.mode.addItem(label, value)
        self.start_button = QPushButton('Начать')
        self.stop_button = QPushButton('Остановить')
        self.stop_button.setEnabled(False)
        controls.addWidget(self.mode, 1)
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)
        if controller:
            self.start_button.clicked.connect(lambda: controller.start(self.mode.currentData()))
            self.stop_button.clicked.connect(controller.stop)
        self.tray = self._tray()
        # GNOME can lack a tray host; retain an accessible window in that case.
        self.hide_button.setEnabled(self.tray is not None)

    def _tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return None
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#e7edf5'))
        painter.drawRoundedRect(4, 4, 24, 20, 5, 5)
        painter.setBrush(QColor('#354150'))
        for y in [9, 14, 19]:
            painter.drawRoundedRect(9, y, 14, 2, 1, 1)
        painter.end()
        tray = QSystemTrayIcon(QIcon(pixmap), self)
        tray.setToolTip('Сводка встречи')
        menu = QMenu()
        show = menu.addAction('Показать сводку')
        show.triggered.connect(self.show_overlay)
        if self.controller:
            self.tray_start = menu.addAction('Начать запись')
            self.tray_start.triggered.connect(lambda: self.controller.start(self.mode.currentData()))
            self.tray_stop = menu.addAction('Остановить запись')
            self.tray_stop.setEnabled(False)
            self.tray_stop.triggered.connect(self.controller.stop)
            menu.addSeparator()
            quit_action = menu.addAction('Завершить')
            quit_action.triggered.connect(self.controller.quit)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self.show_overlay() if reason == QSystemTrayIcon.Trigger else None)
        tray.show()
        self.tray_menu = menu
        return tray

    def show_overlay(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def set_busy(self, busy, *, recording=False):
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(recording)
        self.mode.setEnabled(not busy)
        if self.tray and self.controller:
            self.tray_start.setEnabled(not busy)
            self.tray_stop.setEnabled(recording)

    def update_summary(self, text, timestamp):
        if not text or timestamp < self.latest_timestamp:
            return
        self.latest_timestamp = timestamp
        scroll = self.summary.verticalScrollBar()
        value = scroll.value()
        self.summary.setPlainText(text)
        scroll.setValue(value)

    def closeEvent(self, event):
        if self.tray:
            self.hide()
            event.ignore()
        elif self.controller:
            event.ignore()
            self.controller.quit()
        else:
            event.accept()


class Controller(QObject):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.events = Events()
        self.events.received.connect(self.on_event)
        self.window = Overlay(self)
        self.pipeline = None
        self.busy = False
        self.closing = False
        self.cancel = threading.Event()
        self.settings = QSettings('meeting-summary', 'linux')
        mode = self.settings.value('capture_mode', 'both')
        self.window.mode.setCurrentIndex(max(0, self.window.mode.findData(mode)))
        self.engines = None

    def emit(self, event, **data):
        self.events.received.emit(event, data)

    def start(self, mode):
        if self.busy:
            return
        self.busy = True
        self.cancel.clear()
        self.settings.setValue('capture_mode', mode)
        self.window.set_busy(True)
        self.window.status.setText('Подготовка моделей…')
        threading.Thread(target=self._start, args=(mode,), name='setup', daemon=True).start()

    def _start(self, mode):
        try:
            if self.engines is None:
                from .models import ensure_models
                def progress(name, done, total):
                    if self.cancel.is_set():
                        raise InterruptedError('Загрузка остановлена')
                    self.emit('status', text=f'Первая загрузка моделей: {round(done*100/total)}% · {name}')
                files = ensure_models(progress=progress)
                if self.cancel.is_set():
                    raise InterruptedError('Запуск остановлен')
                self.emit('status', text='Загрузка моделей в память…')
                self.engines = ProcessEngine('speech', files), ProcessEngine('summary', files)
            if self.cancel.is_set():
                raise InterruptedError('Запуск остановлен')
            self.pipeline = MeetingPipeline(*self.engines, self.emit)
            self.pipeline.start(mode)
        except BaseException as error:
            self.emit('startup_failed', text=str(error))

    def stop(self):
        self.cancel.set()
        self.window.set_busy(True)
        self.window.status.setText('Остановка и сохранение оставшейся речи…')
        if self.pipeline:
            threading.Thread(target=self.pipeline.stop, name='stop', daemon=True).start()

    def quit(self):
        self.closing = True
        if not self.busy:
            self.app.quit()
            return
        self.stop()
        self.window.show_overlay()

    @Slot(str, dict)
    def on_event(self, event, data):
        if event == 'summary':
            self.window.update_summary(data['text'], data['timestamp'])
        elif event == 'recording':
            self.window.set_busy(True, recording=True)
            self.window.status.setText('Идёт запись')
            if self.cancel.is_set():
                self.stop()
        elif event == 'transcript':
            backlog = data.get('backlog_seconds', 0)
            self.window.status.setText(f'Идёт запись · отставание расшифровки {backlog:g} с' if backlog > 12 else 'Идёт запись')
        elif event in ('status', 'error'):
            self.window.status.setText(data['text'])
            if event == 'error':
                self.window.show_overlay()
        elif event in ('saved', 'startup_failed'):
            self.busy = False
            self.pipeline = None
            self.engines = None
            self.window.set_busy(False)
            self.window.status.setText(('Расшифровка неполная; полное аудио сохранено' if data.get('incomplete') else 'Аудио и текст сохранены') if event == 'saved' else 'Ошибка запуска: '+data['text'])
            if self.closing:
                self.app.quit()


def run():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    lock = QLockFile(str(Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / f'meeting-summary-{os.getuid()}.lock'))
    if not lock.tryLock(100):
        raise RuntimeError('Сводка встречи уже запущена')
    controller = Controller(app)
    controller.window.show()
    return app.exec()
