import AppKit
import QuartzCore
import AVFoundation
import CoreAudio

let root = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/MeetingOverlay")
final class OverlayPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class OverlayScroller: NSScroller {
    var onInteraction: (() -> Void)?
    override func mouseDown(with event: NSEvent) {
        onInteraction?()
        super.mouseDown(with: event)
    }
}

final class OverlayTextView: NSTextView {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    override func scrollWheel(with event: NSEvent) { enclosingScrollView?.scrollWheel(with: event) }
    override func mouseDown(with event: NSEvent) { window?.performDrag(with: event) }
}

final class SummaryView: NSScrollView {
    let textView = OverlayTextView(frame: .zero)
    private var animateUpdate = true
    var fontSize: CGFloat = 19
    var stringValue: String {
        get { textView.string }
        set {
            guard newValue != textView.string else { return }
            let oldOrigin = contentView.bounds.origin
            textView.layer?.removeAllAnimations()
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineSpacing = 5; paragraph.paragraphSpacing = 12
            let shadow = NSShadow()
            shadow.shadowColor = NSColor.black.withAlphaComponent(0.95)
            shadow.shadowBlurRadius = 3; shadow.shadowOffset = NSSize(width: 0, height: -1)
            textView.textStorage?.setAttributedString(NSAttributedString(string: newValue, attributes: [
                .font: NSFont.systemFont(ofSize: fontSize, weight: .medium),
                .foregroundColor: NSColor.white, .paragraphStyle: paragraph,
                .shadow: shadow, .strokeColor: NSColor.black.withAlphaComponent(0.65), .strokeWidth: -1.5
            ]))
            resizeDocument()
            contentView.scroll(to: animateUpdate ? .zero : oldOrigin); reflectScrolledClipView(contentView)
            guard !newValue.isEmpty else { return }
            if animateUpdate && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                // A fresh five-minute snapshot moves gently up into place, without accumulating old summaries.
                let movement = CABasicAnimation(keyPath: "transform.translation.y")
                movement.fromValue = 28; movement.toValue = 0; movement.duration = 2.4
                movement.timingFunction = CAMediaTimingFunction(name: .easeOut)
                textView.layer?.add(movement, forKey: "newSummary")
                let fade = CABasicAnimation(keyPath: "opacity")
                fade.fromValue = 0; fade.toValue = 1; fade.duration = 0.7
                textView.layer?.add(fade, forKey: "fadeSummary")

            }
        }
    }
    private func resizeDocument() {
        let width = max(1, contentView.bounds.width)
        textView.setFrameSize(NSSize(width: width, height: max(1, textView.frame.height)))
        textView.textContainer?.containerSize = NSSize(width: width, height: .greatestFiniteMagnitude)
        if let container = textView.textContainer, let manager = textView.layoutManager {
            manager.ensureLayout(for: container)
            let height = max(contentView.bounds.height, ceil(manager.usedRect(for: container).height) + 12)
            textView.setFrameSize(NSSize(width: width, height: height))
        }
    }
    override func layout() { super.layout(); resizeDocument() }
    private func stopAutomaticScroll() { textView.layer?.removeAllAnimations() }
    override func scrollWheel(with event: NSEvent) {
        stopAutomaticScroll()
        super.scrollWheel(with: event)
    }
    func changeFont(by amount: CGFloat) {
        fontSize = min(30, max(14, fontSize + amount))
        let current = stringValue
        animateUpdate = false
        textView.string = ""
        stringValue = current
        animateUpdate = true
    }
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        drawsBackground = false; hasVerticalScroller = true; hasHorizontalScroller = false
        scrollerStyle = .overlay; autohidesScrollers = true
        let scroller = OverlayScroller()
        scroller.scrollerStyle = .overlay
        scroller.onInteraction = { [weak self] in self?.stopAutomaticScroll() }
        verticalScroller = scroller
        verticalScrollElasticity = .automatic
        borderType = .noBorder
        textView.isEditable = false; textView.isSelectable = false; textView.isRichText = false
        textView.drawsBackground = false; textView.wantsLayer = true
        textView.textContainerInset = NSSize(width: 0, height: 6)
        textView.minSize = .zero; textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.isVerticallyResizable = true; textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: 596, height: CGFloat.greatestFiniteMagnitude)
        documentView = textView
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
}

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate {
    var panel: OverlayPanel!
    var statusItem: NSStatusItem!
    let statusLine = NSMenuItem(title: "Готово · звук не захватывается", action: nil, keyEquivalent: "")
    let speechAgeLine = NSMenuItem(title: "Последняя речь: ещё не получена", action: nil, keyEquivalent: "")
    let summaryAgeLine = NSMenuItem(title: "Сводка: ещё не сформирована", action: nil, keyEquivalent: "")
    var freshness = SummaryFreshness()
    var statusText = "Готово · звук не захватывается" {
        didSet { statusLine.title = statusText; refreshFreshness() }
    }
    var preview = false
    var overlayHidden = false
    let visibilityItem = NSMenuItem(title: "Скрыть сводку", action: nil, keyEquivalent: "")
    let summary = SummaryView(frame: .zero)
    let startButton = NSMenuItem(title: "Начать", action: nil, keyEquivalent: "s")
    var asr: WorkerClient?
    var summarizer: WorkerClient?
    var asrReady = false
    var summaryReady = false
    var inFlight: AudioChunk?
    var pending = [AudioChunk]()
    var spool: AudioSpool?
    var transcriptFile: URL?
    var summaryInFlight: (id: Int, timestamp: Double)?
    var latestContext: (text: String, timestamp: Double, id: Int)?
    var lastSubmittedContext = ""
    var asrSentAt = Date.distantPast
    var summarySentAt = Date.distantPast
    var lastSummaryRequest = Date.distantPast
    var failures = ["transcribe": 0, "summarize": 0]
    let diagnostics = Diagnostics(directory: root.appendingPathComponent("runtime"))
    var tap: CaptureController?
    var captureMode = CaptureMode(rawValue: UserDefaults.standard.string(forKey: "captureMode") ?? "system") ?? .system
    var selectedMicrophoneUID: String? = UserDefaults.standard.string(forKey: "microphoneUID")
    let sourceMenu = NSMenu(title: "Источник звука")
    let microphoneMenu = NSMenu(title: "Микрофон")
    let microphoneToggle = NSMenuItem(title: "Исключить микрофон из сводки", action: nil, keyEquivalent: "m")
    var running = false
    var ready = false
    var draining = false
    var captureDrained = false
    var reconnecting = false
    var captureFailures = 0
    var drainStarted = Date.distantPast
    var quitAfterDrain = false
    var shutdownUntil = Date.distantPast
    var preservePending = false
    var session: URL?
    var generation = UUID()
    var lastAudio = Date.distantPast
    var lastCallback = Date.distantPast
    var lastReconnect = Date.distantPast
    var lastSummary = Date.distantPast
    var lastSpeechTimestamp = 0.0
    var lastError = Date.distantPast
    var timer: Timer?
    var demo = false
    var player: AVAudioPlayer?
    var hasSummary = false
    var checkingAudio = false
    var testPeak: Float = 0
    var testLastUpdate = Date.distantPast
    var qaEnd: Date?
    var qaRepeat = false
    var qaStopPending = false
    var pendingRoot: URL { root.appendingPathComponent("runtime/pending", isDirectory: true) }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if let url = Bundle.main.url(forResource: "MeetingSummary", withExtension: "icns"),
           let icon = NSImage(contentsOf: url) {
            NSApp.applicationIconImage = icon
        }
        panel = OverlayPanel(contentRect: NSRect(x: 0, y: 0, width: 640, height: 310), styleMask: [.borderless, .resizable, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "Сводка встречи"
        panel.isFloatingPanel = true; panel.hidesOnDeactivate = false
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.minSize = NSSize(width: 360, height: 140)
        panel.isOpaque = false; panel.hasShadow = false
        panel.backgroundColor = NSColor.black.withAlphaComponent(0.08)
        panel.appearance = NSAppearance(named: .darkAqua); panel.delegate = self
        let content = NSView()
        panel.contentView = content
        summary.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(summary)
        NSLayoutConstraint.activate([
            summary.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
            summary.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -22),
            summary.topAnchor.constraint(equalTo: content.topAnchor, constant: 12),
            summary.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -12)
        ])
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "Сводка"; statusItem.button?.toolTip = statusText
        statusItem.button?.image = MeetingIcon.menuBar()
        statusItem.button?.imagePosition = .imageLeading
        let menu = NSMenu(); menu.autoenablesItems = false
        statusLine.isEnabled = false; menu.addItem(statusLine)
        speechAgeLine.isEnabled = false; summaryAgeLine.isEnabled = false
        menu.addItem(speechAgeLine); menu.addItem(summaryAgeLine); menu.addItem(.separator())
        startButton.target = self; startButton.action = #selector(toggle); menu.addItem(startButton)
        visibilityItem.target = self; visibilityItem.action = #selector(toggleVisibility); menu.addItem(visibilityItem)
        menu.addItem(.separator())
        sourceMenu.autoenablesItems = false; sourceMenu.delegate = self
        for mode in CaptureMode.allCases {
            let item = NSMenuItem(title: mode.title, action: #selector(selectSource(_:)), keyEquivalent: "")
            item.representedObject = mode.rawValue; item.target = self; sourceMenu.addItem(item)
        }
        let sourceItem = NSMenuItem(title: "Источник звука", action: nil, keyEquivalent: ""); sourceItem.submenu = sourceMenu; menu.addItem(sourceItem)
        microphoneMenu.autoenablesItems = false; microphoneMenu.delegate = self
        let deviceItem = NSMenuItem(title: "Выбрать микрофон", action: nil, keyEquivalent: ""); deviceItem.submenu = microphoneMenu; menu.addItem(deviceItem)
        microphoneToggle.target = self; microphoneToggle.action = #selector(toggleMicrophone); menu.addItem(microphoneToggle)
        menu.addItem(.separator())
        for (name, action) in [("Текст крупнее", #selector(largerText)), ("Текст мельче", #selector(smallerText)), ("Вернуть окно на экран", #selector(resetPosition)), ("Открыть расшифровки", #selector(openTranscripts)), ("Дописать сохранённые фрагменты", #selector(recoverPending)), ("Открыть журнал работы", #selector(openDiagnostics)), ("Проверить отображение", #selector(previewOverlay)), ("Проверить на примере со звуком", #selector(testDemo)), ("Проверить только звук", #selector(checkAudio))] {
            let item = NSMenuItem(title: name, action: action, keyEquivalent: action == #selector(previewOverlay) ? "p" : ""); item.target = self; menu.addItem(item)
        }
        menu.addItem(.separator())
        let quitItem = NSMenuItem(title: "Завершить", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self; menu.addItem(quitItem)
        menu.delegate = self; statusItem.menu = menu
        let mainMenu = NSMenu(); let appMenu = NSMenuItem()
        appMenu.submenu = menu.copy() as? NSMenu
        appMenu.submenu?.delegate = self
        mainMenu.addItem(appMenu); NSApp.mainMenu = mainMenu
        refreshSourceMenus()
        resetPosition()
        if !UserDefaults.standard.bool(forKey: "didPreviewTransparentOverlay") && ProcessInfo.processInfo.environment["MEETING_QA_SECONDS"] == nil {
            UserDefaults.standard.set(true, forKey: "didPreviewTransparentOverlay")
            DispatchQueue.main.async { [weak self] in self?.previewOverlay() }
        }
        timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(timer!, forMode: .common)
        NSWorkspace.shared.notificationCenter.addObserver(self, selector: #selector(woke), name: NSWorkspace.didWakeNotification, object: nil)
        if let value = ProcessInfo.processInfo.environment["MEETING_QA_SECONDS"], let seconds = Double(value), seconds > 0 {
            if let source = ProcessInfo.processInfo.environment["MEETING_QA_SOURCE"], let mode = CaptureMode(rawValue: source) { captureMode = mode }
            demo = true; qaRepeat = true; qaEnd = Date().addingTimeInterval(seconds)
            begin()
        } else if ProcessInfo.processInfo.arguments.contains("--preview") { previewOverlay() }
        else if !recoverableSessions().isEmpty { statusText = "Есть несохранённые фрагменты · выберите «Дописать сохранённые фрагменты»" }
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if summary.stringValue.isEmpty { statusItem.button?.performClick(nil) } else { showPanel() }
        return false
    }
    func refreshSourceMenus() {
        for item in sourceMenu.items {
            item.state = (item.representedObject as? String) == captureMode.rawValue ? .on : .off
            item.isEnabled = !running
        }
        let devices = [(uid: "", name: "Как в настройках macOS")] + MicrophoneCapture.devices()
        let missing = selectedMicrophoneUID.map { uid in !devices.contains { $0.uid == uid } } ?? false
        let current = microphoneMenu.items.compactMap { $0.representedObject as? String }
        if current != devices.map(\.uid) || microphoneMenu.items.count != devices.count + (missing ? 1 : 0) {
            microphoneMenu.removeAllItems()
            for device in devices {
                let item = NSMenuItem(title: device.name, action: #selector(selectMicrophone(_:)), keyEquivalent: "")
                item.representedObject = device.uid; item.target = self; microphoneMenu.addItem(item)
            }
            if missing {
                let item = NSMenuItem(title: "Выбранный микрофон сейчас не подключён", action: nil, keyEquivalent: "")
                item.isEnabled = false; microphoneMenu.addItem(item)
            }
        }
        for (item, device) in zip(microphoneMenu.items, devices) {
            item.title = device.name
            item.state = device.uid == (selectedMicrophoneUID ?? "") ? .on : .off
            item.isEnabled = !running
        }
        microphoneToggle.isEnabled = running && !draining && captureMode.sources.contains(.microphone)
        microphoneToggle.title = tap?.microphoneMuted == true ? "Включить микрофон в сводку" : "Исключить микрофон из сводки"
    }
    func menuWillOpen(_ menu: NSMenu) {
        refreshFreshness()
        refreshSourceMenus()
        guard let main = statusItem.menu else { return }
        let original: NSMenu
        if menu.title == sourceMenu.title { original = sourceMenu }
        else if menu.title == microphoneMenu.title { original = microphoneMenu }
        else { original = main }
        guard menu !== original else { return }
        let sameItems = menu.items.count == original.items.count && zip(menu.items, original.items).allSatisfy {
            ($0.representedObject as? String) == ($1.representedObject as? String) && $0.action == $1.action
        }
        if sameItems {
            // Preserve item identity while opening submenus, including AX actions.
            for (copy, item) in zip(menu.items, original.items) {
                copy.title = item.title; copy.state = item.state; copy.isEnabled = item.isEnabled
            }
        } else {
            menu.removeAllItems()
            for item in original.items { if let copy = item.copy() as? NSMenuItem { menu.addItem(copy) } }
        }
    }
    @objc func selectSource(_ sender: NSMenuItem) {
        guard !running, let value = sender.representedObject as? String, let mode = CaptureMode(rawValue: value) else { return }
        captureMode = mode; UserDefaults.standard.set(mode.rawValue, forKey: "captureMode")
        statusText = "Источник: \(mode.title) · запись выключена"; refreshSourceMenus()
    }
    @objc func selectMicrophone(_ sender: NSMenuItem) {
        guard !running, let value = sender.representedObject as? String else { return }
        selectedMicrophoneUID = value.isEmpty ? nil : value
        UserDefaults.standard.set(selectedMicrophoneUID, forKey: "microphoneUID")
        statusText = "Микрофон: \(sender.title) · запись выключена"; refreshSourceMenus()
    }
    @objc func toggleMicrophone() {
        guard running, !draining, captureMode.sources.contains(.microphone), let tap else { return }
        tap.setMicrophoneMuted(!tap.microphoneMuted); statusText = tap.statusDescription; refreshSourceMenus()
    }
    @objc func showPanel() { overlayHidden = false; visibilityItem.title = "Скрыть сводку"; if !summary.stringValue.isEmpty { panel.orderFrontRegardless() } }
    @objc func hidePanel() { overlayHidden = true; visibilityItem.title = "Показать сводку"; panel.orderOut(nil) }
    @objc func toggleVisibility() { if overlayHidden { showPanel() } else { hidePanel() } }
    var transcriptsFolder: URL {
        FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("Расшифровки встреч", isDirectory: true)
    }
    @objc func openTranscripts() {
        do {
            try FileManager.default.createDirectory(at: transcriptsFolder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            NSWorkspace.shared.open(transcriptsFolder)
        } catch { statusText = "Не удалось открыть расшифровки: \(error.localizedDescription)" }
    }
    @objc func largerText() { summary.changeFont(by: 2) }
    @objc func smallerText() { summary.changeFont(by: -2) }
    @objc func resetPosition() {
        if let screen = NSScreen.main {
            panel.setFrame(NSRect(x: screen.visibleFrame.maxX - 670, y: screen.visibleFrame.minY + 70, width: 640, height: 310), display: true)
        }
    }
    func refreshFreshness() {
        let labels = freshness.labels(now: Date(), preview: preview)
        speechAgeLine.title = labels.speech; summaryAgeLine.title = labels.summary
        statusItem?.button?.toolTip = statusText + "\n" + labels.speech + "\n" + labels.summary
    }
    func presentSummary(_ text: String) {
        if text.isEmpty { freshness.clearDisplayed(); refreshFreshness() }
        panel.contentView?.layoutSubtreeIfNeeded()
        summary.layoutSubtreeIfNeeded()
        summary.stringValue = text
        if text.isEmpty { panel.orderOut(nil) }
        else if !overlayHidden { panel.orderFrontRegardless() }
    }
    @objc func previewOverlay() {
        guard !running else { stop(); return }
        stop(); preview = true; showPanel()
        statusText = "Проверка отображения · звук не захватывается"
        statusItem.button?.title = "Сводка · пример"
        startButton.title = "Завершить просмотр"
        let thisGeneration = generation
        presentSummary("• Обсудили новый экран приложения: поверх остальных окон останется только краткая сводка.\n• Управление переносится в строку меню macOS.\n• Текст должен читаться на светлом и тёмном фоне.")
        DispatchQueue.main.asyncAfter(deadline: .now() + 7) { [weak self] in
            guard let self, self.preview, self.generation == thisGeneration else { return }
            self.presentSummary("• Согласовали почти прозрачный фон и крупный текст с тенью.\n• Новая сводка плавно поднимается при появлении. После появления текст остаётся неподвижным; длинную сводку можно прокрутить вручную.\n• Расшифровка, кнопки и служебные сообщения скрыты из окна. Запуск и остановка доступны в строке меню macOS.\n• Сводка охватывает последние пять минут разговора. История всей встречи в этом окне не накапливается.\n• Окно можно перетащить за текст и изменить его размер за край. Размер шрифта меняется через меню «Сводка».")
        }
    }
    func windowShouldClose(_ sender: NSWindow) -> Bool { hidePanel(); return false }
    @objc func quit() { NSApp.terminate(nil) }
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if running { quitAfterDrain = true; stop(); return .terminateLater }
        if Date() < shutdownUntil {
            quitAfterDrain = true
            DispatchQueue.main.asyncAfter(deadline: .now() + shutdownUntil.timeIntervalSinceNow) { NSApp.reply(toApplicationShouldTerminate: true) }
            return .terminateLater
        }
        return .terminateNow
    }
    func applicationWillTerminate(_ notification: Notification) { asr?.stop(); summarizer?.stop(); tap?.stop() }
    @objc func toggle() { if running || preview { stop() } else { begin() } }
    @objc func testDemo() { guard !running else { return }; demo = true; begin(); showPanel() }
    @objc func checkAudio() {
        guard !quitAfterDrain else { return }
        guard !running else { stop(); return }
        stop()
        generation = UUID(); let thisGeneration = generation
        checkingAudio = true; running = true; ready = false; testPeak = 0
        draining = false; captureDrained = false; preservePending = false
        startButton.title = "Остановить"
        statusText = "Проверка: \(captureMode.title)…"
        let capture = CaptureController(mode: captureMode, microphoneUID: selectedMicrophoneUID)
        capture.onSamples = { [weak self] values, rate, _ in DispatchQueue.main.async {
            guard let self, self.generation == thisGeneration, self.checkingAudio else { return }
            let peak = values.map { abs($0) }.max() ?? 0
            self.testPeak = max(self.testPeak, peak)
            if peak > 0.002 { self.lastAudio = Date() }
            if Date().timeIntervalSince(self.testLastUpdate) > 1 {
                self.testLastUpdate = Date()
                self.statusText = self.testPeak > 0.002 ? "Звук получен: \(self.captureMode.title)." : "Ожидаю звук: \(self.captureMode.title)."
            }
        } }
        capture.onStatus = { [weak self] status in
            guard let self, self.generation == thisGeneration else { return }; self.statusText = status
        }
        tap = capture
        capture.start { [weak self] result in
            guard let self, self.generation == thisGeneration, self.running else { capture.stop(); return }
            switch result {
            case .success:
                self.ready = true; self.lastAudio = Date()
                self.statusText = "Проверка: \(self.captureMode.title) · без сохранения"
                self.statusItem.button?.title = "● Сводка"
                DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
                    guard let self, self.generation == thisGeneration, self.captureMode.sources.contains(.system) else { return }; self.playDemo()
                }
            case .failure(let error): self.stop(); self.statusText = error.localizedDescription
            }
        }
    }
    func playDemo() {
        do {
            let audioURL = ProcessInfo.processInfo.environment["MEETING_QA_AUDIO"].map { URL(fileURLWithPath: $0) } ?? Bundle.main.url(forResource: "meeting-sample", withExtension: "wav")!
            player = try AVAudioPlayer(contentsOf: audioURL); if let uid = tap?.outputUID, !uid.isEmpty { player?.currentDevice = uid }; player?.numberOfLoops = qaRepeat ? -1 : 0; player?.prepareToPlay(); player?.play(); demo = false }
        catch { statusText = "Нет тестового аудио: \(error.localizedDescription)" }
    }
    func begin() {
        guard !running, !quitAfterDrain else { return }
        freshness.reset(); refreshFreshness()
        preview = false; showPanel(); generation = UUID()
        running = true; ready = false; draining = false; captureDrained = false; preservePending = false
        captureFailures = 0; hasSummary = false; pending.removeAll(); inFlight = nil; latestContext = nil
        summaryInFlight = nil; lastSubmittedContext = ""; lastSummaryRequest = .distantPast
        failures = ["transcribe": 0, "summarize": 0]; lastSpeechTimestamp = 0
        startButton.title = "Остановить"; startButton.isEnabled = true
        presentSummary(""); statusText = "Подключение источника и загрузка моделей…"
        let directory = pendingRoot.appendingPathComponent(UUID().uuidString, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            try FileManager.default.createDirectory(at: transcriptsFolder, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
            let formatter = DateFormatter(); formatter.dateFormat = "yyyy-MM-dd HH-mm-ss"
            let prefix = demo ? "Тест — " : "Встреча — "
            let file = transcriptsFolder.appendingPathComponent(prefix + formatter.string(from: Date()) + " — " + UUID().uuidString.prefix(8) + ".txt")
            transcriptFile = file; session = directory
            let manifest: [String: Any] = ["transcript": file.path, "started": Date().timeIntervalSince1970, "source": captureMode.rawValue]
            try JSONSerialization.data(withJSONObject: manifest).write(to: directory.appendingPathComponent("session.json"), options: .atomic)
            let spool = AudioSpool(directory: directory); self.spool = spool
            let token = generation
            spool.onChunk = { [weak self] chunk in
                guard let self, self.generation == token else { return }
                self.pending.append(chunk)
                self.diagnostic(["event": "queued", "id": chunk.id, "queue": self.pending.count, "duration": chunk.duration])
                self.dispatchAudio()
                if self.pending.reduce(0.0, { $0 + $1.duration }) > 900 && !self.draining {
                    self.lastError = Date(); self.statusText = "Обработка отстаёт на 15 минут · сохраняю очередь и останавливаю захват"
                    self.stop()
                }
            }
            spool.onError = { [weak self] error in
                guard let self, self.generation == token else { return }
                self.failSession("Не удалось сохранить звук: \(error.localizedDescription)")
            }
            diagnostic(["event": "session_start", "session": directory.lastPathComponent, "source": captureMode.rawValue])
            startCapture(); startWorker("transcribe"); startWorker("summarize")
        } catch { failSession("Не удалось запустить: \(error.localizedDescription)") }
    }
    func diagnostic(_ fields: [String: Any]) { diagnostics.record(fields) }
    func startWorker(_ mode: String) {
        guard running, let session else { return }
        if mode == "summarize" && draining { return }
        let client = WorkerClient(mode: mode, session: session, transcript: mode == "transcribe" ? transcriptFile : nil,
                                  resume: mode == "transcribe" && FileManager.default.fileExists(atPath: transcriptFile?.path ?? ""))
        if mode == "transcribe" { asr = client; asrReady = false } else { summarizer = client; summaryReady = false }
        let token = generation
        client.onEvent = { [weak self, weak client] object in
            guard let self, let client, self.running, self.generation == token,
                  (mode == "transcribe" ? self.asr === client : self.summarizer === client) else { return }
            self.receive(object, mode: mode)
        }
        client.onExit = { [weak self, weak client] code in
            guard let self, let client, self.running, self.generation == token,
                  (mode == "transcribe" ? self.asr === client : self.summarizer === client) else { return }
            self.workerFailed(mode, reason: "Процесс завершился: \(code)")
        }
        do { try client.start(); diagnostic(["event": "worker_start", "stage": mode]) }
        catch { workerFailed(mode, reason: error.localizedDescription) }
    }
    func workerFailed(_ mode: String, reason: String) {
        guard running else { return }
        diagnostic(["event": "worker_failure", "stage": mode, "reason": reason])
        failures[mode, default: 0] += 1; lastError = Date()
        if mode == "transcribe" { asr?.stop(); asr = nil; asrReady = false; inFlight = nil }
        else { summarizer?.stop(); summarizer = nil; summaryReady = false; summaryInFlight = nil; lastSubmittedContext = "" }
        if failures[mode, default: 0] > 3 {
            if mode == "transcribe" { failSession("Распознавание остановлено. Фрагменты сохранены; используйте «Дописать сохранённые фрагменты».") }
            else { statusText = "Сводка временно недоступна · расшифровка продолжает сохраняться" }
            return
        }
        statusText = mode == "transcribe" ? "Восстанавливаю распознавание · звук сохраняется" : "Восстанавливаю сводку · расшифровка продолжается"
        let token = generation
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
            guard let self, self.running, self.generation == token else { return }; self.startWorker(mode)
        }
    }
    func receive(_ object: [String: Any], mode: String) {
        let event = object["event"] as? String ?? "unknown"
        diagnostic(["event": event, "stage": mode, "id": object["request_id"] ?? -1,
                    "seconds": object["seconds"] ?? 0, "queue": pending.count,
                    "hasSummary": !(object["summary"] as? String ?? "").isEmpty,
                    "validationFailed": object["validation_failed"] as? Bool ?? false])
        if event == "ready" {
            if mode == "transcribe" { asrReady = true; dispatchAudio() }
            else { summaryReady = true; dispatchSummary() }
            if !draining {
                statusText = tap?.isCapturing == true ? "\(tap!.statusDescription) · сводка последних 5 минут" : "Модель готова · \(tap?.statusDescription ?? "ожидаю подключения звука")"
            }
        } else if event == "result" {
            let id = object["request_id"] as? Int
            if mode == "transcribe" {
                guard let chunk = inFlight, id == chunk.id else { diagnostic(["event": "stale_result", "stage": mode]); return }
                failures[mode] = 0
                chunk.acknowledge(); pending.removeAll { $0.id == chunk.id }; inFlight = nil
                if let text = object["summary_context"] as? String, !text.isEmpty {
                    let timestamp = object["context_timestamp"] as? Double ?? chunk.timestamp
                    latestContext = (text, timestamp, chunk.id)
                    if !(object["transcript"] as? String ?? "").isEmpty {
                        lastSpeechTimestamp = timestamp
                        freshness.recordSpeech(at: Date(timeIntervalSince1970: timestamp)); refreshFreshness()
                    }
                }
                dispatchAudio(); dispatchSummary()
            } else {
                guard let request = summaryInFlight, id == request.id else { return }
                failures[mode] = 0; summaryInFlight = nil
                if let text = object["summary"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                   Date().timeIntervalSince1970 - request.timestamp < 300, !draining {
                    let shownAt = Date()
                    freshness.publish(source: Date(timeIntervalSince1970: request.timestamp), displayed: shownAt)
                    presentSummary(text); hasSummary = true; lastSummary = shownAt
                    refreshFreshness()
                    statusText = "Захват включён · сводка обновлена \(DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .medium))"
                } else if !draining && object["validation_failed"] as? Bool == true {
                    freshness.reject(); refreshFreshness()
                    statusText = "Новая сводка не прошла проверку · расшифровка сохраняется"
                } else if !hasSummary && !draining { statusText = "Речь сохраняется · ожидаю содержательные реплики для сводки" }
                dispatchSummary()
            }
        } else if event == "error" || event == "protocol_error" {
            workerFailed(mode, reason: object["text"] as? String ?? "Ошибка обмена с обработчиком")
        }
    }
    func dispatchAudio() {
        guard running else { return }
        if draining && captureDrained && pending.isEmpty && inFlight == nil { finishStop(); return }
        guard asrReady, inFlight == nil, let chunk = pending.first, let asr else { return }
        do { inFlight = chunk; asrSentAt = Date(); try asr.send(chunk.request) }
        catch { workerFailed("transcribe", reason: error.localizedDescription) }
    }
    func dispatchSummary() {
        guard running, !draining, summaryReady, summaryInFlight == nil, let context = latestContext,
              let summarizer else { return }
        guard SummaryRequestPolicy.allows(text: context.text, sourceTimestamp: context.timestamp,
                                          now: Date().timeIntervalSince1970,
                                          lastRequested: lastSummaryRequest.timeIntervalSince1970,
                                          lastSubmittedText: lastSubmittedContext) else { return }
        do {
            summaryInFlight = (context.id, context.timestamp); summarySentAt = Date(); lastSummaryRequest = Date()
            lastSubmittedContext = context.text
            try summarizer.send(["text": context.text, "timestamp": context.timestamp, "request_id": context.id])
        } catch { workerFailed("summarize", reason: error.localizedDescription) }
    }
    func startCapture() {
        guard running, !draining else { return }
        let capture = CaptureController(mode: captureMode, microphoneUID: selectedMicrophoneUID)
        tap = capture; let token = generation
        lastCallback = Date(); lastReconnect = Date(); reconnecting = true
        capture.onSamples = { [weak self] values, rate, timestamp in
            DispatchQueue.main.async {
                guard let self, self.running, self.generation == token else { return }
                self.lastCallback = Date()
                if values.contains(where: { abs($0) > 0.002 }) { self.lastAudio = Date() }
                self.spool?.add(values, rate: rate, timestamp: timestamp)
            }
        }
        capture.onStatus = { [weak self] status in
            guard let self, self.running, !self.draining, self.generation == token else { return }
            self.statusText = status; self.refreshSourceMenus()
        }
        capture.onDiagnostic = { [weak self] event in self?.diagnostic(event) }
        capture.start { [weak self] result in
            guard let self, self.running, !self.draining, self.generation == token else { capture.stop(); return }
            self.reconnecting = false
            switch result {
            case .success:
                self.ready = true; self.lastCallback = Date()
                self.statusItem.button?.title = "● Сводка"
                self.diagnostic(["event": "capture_started", "source": self.captureMode.rawValue])
                self.refreshSourceMenus()
                if self.demo { self.playDemo() }
            case .failure(let error): self.failSession(error.localizedDescription)
            }
        }
    }
    @objc func woke() { tap?.wake() }
    func tick() {
        refreshFreshness()
        guard running else { return }
        if draining && Date().timeIntervalSince(drainStarted) > 120 {
            preserveAndFinish("Сохранение задержалось · оставшиеся фрагменты сохранены для восстановления"); return
        }
        if !draining { tap?.tick() }
        guard !checkingAudio else { return }
        if let end = qaEnd, Date() >= end, !qaStopPending { qaStopPending = true; stop() }
        if !asrReady, let asr, Date().timeIntervalSince(asr.startedAt) > 180 { workerFailed("transcribe", reason: "Тайм-аут загрузки") }
        if !summaryReady, let summarizer, Date().timeIntervalSince(summarizer.startedAt) > 180 { workerFailed("summarize", reason: "Тайм-аут загрузки") }
        if inFlight != nil && Date().timeIntervalSince(asrSentAt) > 90 { workerFailed("transcribe", reason: "Тайм-аут распознавания") }
        if summaryInFlight != nil && Date().timeIntervalSince(summarySentAt) > 180 { workerFailed("summarize", reason: "Тайм-аут сводки") }
        if draining {
            dispatchAudio(); return
        }
        if hasSummary && lastSpeechTimestamp > 0 && Date().timeIntervalSince1970 - lastSpeechTimestamp > 300 {
            presentSummary(""); hasSummary = false
        }
        dispatchSummary()
        if Date().timeIntervalSince(lastAudio) > 12 && Date().timeIntervalSince(lastError) > 20 && ready && pending.isEmpty {
            statusText = (tap?.microphoneMuted == true && captureMode == .microphone) ? "Микрофон исключён из сводки · ожидание" : "Ожидаю речь · \(tap?.statusDescription ?? captureMode.title)"
        }
    }
    func stop() {
        preview = false
        guard running else {
            presentSummary(""); statusItem?.button?.title = "Сводка"; startButton.title = "Начать"; return
        }
        guard !draining else { return }
        draining = true; drainStarted = Date(); captureDrained = false; ready = false
        startButton.title = "Сохранение последних реплик…"; startButton.isEnabled = false
        statusText = "Останавливаю захват · сохраняю последние реплики"
        player?.stop(); player = nil; demo = false; qaRepeat = false
        summarizer?.stop(); summarizer = nil; summaryReady = false; summaryInFlight = nil
        let token = generation
        let drained = { [weak self] in
            guard let self, self.generation == token else { return }
            self.tap = nil
            if let spool = self.spool {
                spool.finish { [weak self] in
                    guard let self, self.generation == token else { return }
                    self.captureDrained = true
                    if self.preservePending { self.finishStop() } else { self.dispatchAudio() }
                }
            } else { self.captureDrained = true; self.finishStop() }
        }
        if let tap { tap.stop(completion: drained) } else { drained() }
    }
    func failSession(_ message: String) {
        preservePending = true; diagnostic(["event": "session_error", "reason": message])
        lastError = Date()
        if draining && captureDrained { finishStop() } else { stop() }
        statusText = message
    }
    func preserveAndFinish(_ message: String) {
        preservePending = true
        diagnostic(["event": "forced_stop", "reason": message])
        tap?.stop(); spool?.finish({})
        finishStop(); statusText = message
    }
    func finishStop() {
        let remainingAudio = session.flatMap { try? FileManager.default.contentsOfDirectory(at: $0, includingPropertiesForKeys: nil) }?.contains { $0.pathExtension == "wav" } ?? false
        let preserved = preservePending || !pending.isEmpty || remainingAudio
        diagnostic(["event": "session_stop", "pending": pending.count, "preserved": preserved])
        running = false; ready = false; checkingAudio = false; draining = false; generation = UUID()
        asr?.stop(); asr = nil; summarizer?.stop(); summarizer = nil
        shutdownUntil = Date().addingTimeInterval(3.5)
        asrReady = false; summaryReady = false; inFlight = nil; summaryInFlight = nil
        tap = nil; spool = nil; pending.removeAll(); refreshSourceMenus()
        if let session, !preserved { try? FileManager.default.removeItem(at: session) }
        session = nil; latestContext = nil
        startButton.title = "Начать"; startButton.isEnabled = true; statusItem?.button?.title = "Сводка"
        statusText = preserved ? "Необработанные фрагменты сохранены · «Дописать сохранённые фрагменты»" : "Остановлено · расшифровка сохранена"
        if qaEnd != nil { diagnostic(["event": "qa_complete", "hasSummary": hasSummary]); qaEnd = nil }
        if quitAfterDrain {
            startButton.isEnabled = false
            DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { NSApp.reply(toApplicationShouldTerminate: true) }
        }
    }
    func recoverableSessions() -> [URL] {
        ((try? FileManager.default.contentsOfDirectory(at: pendingRoot, includingPropertiesForKeys: nil)) ?? []).filter {
            FileManager.default.fileExists(atPath: $0.appendingPathComponent("session.json").path)
        }.sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
    @objc func recoverPending() {
        guard !running else { return }
        guard let directory = recoverableSessions().first else { statusText = "Несохранённых фрагментов нет"; return }
        do {
            let data = try Data(contentsOf: directory.appendingPathComponent("session.json"))
            let manifest = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            guard let path = manifest?["transcript"] as? String else { throw CocoaError(.fileReadCorruptFile) }
            var chunks = [AudioChunk]()
            for wav in try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) where wav.lastPathComponent.hasPrefix("chunk-") && wav.pathExtension == "wav" {
                let metadata = wav.deletingPathExtension().appendingPathExtension("json")
                let record = (try? Data(contentsOf: metadata)).flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
                if let id = record?["request_id"] as? Int, let stamp = record?["timestamp"] as? Double,
                   let duration = record?["duration"] as? Double {
                    chunks.append(AudioChunk(id: id, path: wav, timestamp: stamp, duration: duration))
                } else {
                    // A crash between WAV close and sidecar commit must not erase recoverable audio.
                    guard let id = Int(wav.deletingPathExtension().lastPathComponent.replacingOccurrences(of: "chunk-", with: "")),
                          let file = try? AVAudioFile(forReading: wav), file.length > 0,
                          let attributes = try? FileManager.default.attributesOfItem(atPath: wav.path),
                          let modified = attributes[.modificationDate] as? Date else { continue }
                    let chunk = AudioChunk(id: id, path: wav, timestamp: modified.timeIntervalSince1970,
                                           duration: Double(file.length) / file.fileFormat.sampleRate)
                    var repaired = chunk.request; repaired["duration"] = chunk.duration
                    try JSONSerialization.data(withJSONObject: repaired).write(to: metadata, options: .atomic)
                    chunks.append(chunk)
                    diagnostic(["event": "recovered_orphan_audio", "id": id])
                }
            }
            session = directory; transcriptFile = URL(fileURLWithPath: path); pending = chunks.sorted { $0.id < $1.id }
            running = true; draining = true; drainStarted = Date(); captureDrained = true; preservePending = false; checkingAudio = false
            generation = UUID(); failures["transcribe"] = 0; inFlight = nil
            startButton.title = "Сохранение фрагментов…"; startButton.isEnabled = false
            statusText = "Восстанавливаю расшифровку из сохранённых фрагментов · звук не захватывается"
            if pending.isEmpty { finishStop() } else { startWorker("transcribe") }
        } catch { statusText = "Не удалось восстановить фрагменты: \(error.localizedDescription)" }
    }
    @objc func openDiagnostics() { NSWorkspace.shared.open(root.appendingPathComponent("runtime/diagnostics.jsonl")) }

}
@main struct MeetingMain {
    static func main() {
        let application = NSApplication.shared
        let delegate = App()
        application.delegate = delegate
        application.run()
        withExtendedLifetime(delegate) {}
    }
}
