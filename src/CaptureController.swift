import Foundation
import AVFoundation

enum CaptureMode: String, CaseIterable {
    case system, microphone, both
    var title: String {
        switch self { case .system: return "Звук Mac"; case .microphone: return "Микрофон"; case .both: return "Звук Mac + микрофон" }
    }
    var sources: Set<AudioSource> {
        switch self { case .system: return [.system]; case .microphone: return [.microphone]; case .both: return [.system, .microphone] }
    }
}

protocol CaptureInput: AnyObject {
    var onSamples: (([Float], Double) -> Void)? { get set }
    var onError: ((Error) -> Void)? { get set }
    var captureUID: String { get }
    func start(completion: @escaping (Result<Void, Error>) -> Void)
    func stop(completion: (() -> Void)?)
}

private final class SystemCaptureInput: CaptureInput {
    let tap = AudioTap()
    var onError: ((Error) -> Void)?
    var onSamples: (([Float], Double) -> Void)? { get { tap.onSamples } set { tap.onSamples = newValue } }
    var captureUID: String { tap.outputUID }
    func start(completion: @escaping (Result<Void, Error>) -> Void) { tap.start(completion: completion) }
    func stop(completion: (() -> Void)?) { tap.stop(completion: completion) }
}
extension MicrophoneCapture: CaptureInput { var captureUID: String { deviceUID } }

/// Independent inputs share one timeline. Silence/failure of either never gates the other.
final class CaptureController {
    typealias Factory = (AudioSource, String?) -> CaptureInput
    let mode: CaptureMode
    let selectedMicrophone: String?
    let mixer: AudioMixer
    var onSamples: (([Float], Double, Double) -> Void)?
    var onStatus: ((String) -> Void)?
    var onDiagnostic: (([String: Any]) -> Void)?
    private let factory: Factory
    private let currentUID: (AudioSource) -> String?
    private let authorization: () -> AVAuthorizationStatus
    private let clock: () -> Date
    private var inputs: [AudioSource: CaptureInput] = [:]
    private var sourceGeneration: [AudioSource: UUID] = [:]
    private var restartGeneration: [AudioSource: UUID] = [:]
    private var active = Set<AudioSource>()
    private var starting = Set<AudioSource>()
    private var waitingForPermission = Set<AudioSource>()
    private var failed = Set<AudioSource>()
    private var exhausted = Set<AudioSource>()
    private var muted = false
    private var stopped = false
    private var stopping = false
    private var startedAt: [AudioSource: Date] = [:]
    private var lastCallback: [AudioSource: Date] = [:]
    private var nextRetry: [AudioSource: Date] = [:]
    private var attempts: [AudioSource: Int] = [:]
    private var completion: ((Result<Void, Error>) -> Void)?
    private var initialReported = false
    private var lastPoll = Date.distantPast
    private var messages: [AudioSource: String] = [:]
    var outputUID: String { inputs[.system]?.captureUID ?? "" }
    var inputUID: String { inputs[.microphone]?.captureUID ?? "" }
    var isCapturing: Bool { !active.isEmpty }
    var microphoneMuted: Bool { muted }
    var statusDescription: String {
        if muted && mode == .microphone { return "Микрофон исключён из сводки · ожидание" }
        if mode == .both && active == [.microphone] { return "Записывается микрофон · звук Mac недоступен" }
        if mode == .both && active == [.system] { return muted ? "Записывается звук Mac · микрофон исключён" : "Записывается звук Mac · микрофон недоступен" }
        if !active.isEmpty { return "Запись: \(mode.title)" }
        return messages.values.sorted().first ?? "Подключение: \(mode.title)…"
    }
    init(mode: CaptureMode, microphoneUID: String? = nil, mixer: AudioMixer? = nil,
         factory: Factory? = nil, currentUID: ((AudioSource) -> String?)? = nil, clock: @escaping () -> Date = Date.init,
         authorization: @escaping () -> AVAuthorizationStatus = { AVCaptureDevice.authorizationStatus(for: .audio) }) {
        self.authorization = authorization
        self.mode = mode; selectedMicrophone = microphoneUID; self.clock = clock
        self.mixer = mixer ?? AudioMixer(enabledSources: mode.sources)
        self.factory = factory ?? { source, uid -> CaptureInput in
            if source == .system { return SystemCaptureInput() }
            return MicrophoneCapture(deviceUID: uid)
        }
        self.currentUID = currentUID ?? { source in source == .system ? (try? AudioTap.currentOutputUID()) : (try? MicrophoneCapture.currentDefaultUID()) }
        self.mixer.onSamples = { [weak self] values, rate, timestamp in self?.onSamples?(values, rate, timestamp) }
    }
    func start(completion: @escaping (Result<Void, Error>) -> Void) {
        self.completion = completion
        for source in mode.sources { startSource(source) }
    }
    private func label(_ source: AudioSource) -> String { source == .system ? "Звук Mac" : "Микрофон" }
    private func startSource(_ source: AudioSource) {
        guard !stopped, !stopping, mode.sources.contains(source), !(source == .microphone && muted) else { return }
        let token = UUID(); sourceGeneration[source] = token
        restartGeneration[source] = nil
        let input = factory(source, source == .microphone ? selectedMicrophone : nil)
        inputs[source] = input; starting.insert(source); active.remove(source); failed.remove(source)
        waitingForPermission.remove(source)
        startedAt[source] = clock(); lastCallback[source] = clock(); nextRetry[source] = nil
        var detail: [String: Any] = ["event": "input_start_attempt", "source": label(source)]
        if source == .microphone {
            let authorization = authorization()
            detail["authorization"] = authorization.rawValue
            detail["explicitDevice"] = selectedMicrophone != nil
            if authorization == .notDetermined {
                waitingForPermission.insert(source)
                messages[source] = "Микрофон: ожидаю разрешения macOS"
                onStatus?(statusDescription)
            }
        }
        onDiagnostic?(detail)
        input.onSamples = { [weak self] values, rate in
            guard let self else { return }; let timestamp = self.clock().timeIntervalSince1970
            DispatchQueue.main.async { [weak self] in
                guard let self, !self.stopped, self.sourceGeneration[source] == token,
                      !(source == .microphone && self.muted) else { return }
                self.lastCallback[source] = self.clock(); self.attempts[source] = 0
                self.mixer.add(values, rate: rate, source: source, timestamp: timestamp)
            }
        }
        input.onError = { [weak self] error in DispatchQueue.main.async {
            guard let self, !self.stopped, !self.stopping, self.sourceGeneration[source] == token else { return }
            self.sourceFailed(source, error: error)
        } }
        input.start { [weak self, weak input] result in
            guard let self, input != nil, !self.stopped, !self.stopping, self.sourceGeneration[source] == token else { input?.stop(completion: nil); return }
            switch result {
            case .success:
                self.waitingForPermission.remove(source)
                self.starting.remove(source); self.failed.remove(source); self.active.insert(source)
                self.messages[source] = nil; self.lastCallback[source] = self.clock()
                self.onDiagnostic?(["event": "input_started", "source": self.label(source)])
                if !self.initialReported { self.initialReported = true; self.completion?(.success(())); self.completion = nil }
                self.onStatus?(self.statusDescription)
            case .failure(let error): self.sourceFailed(source, error: error)
            }
        }
    }
    private func sourceFailed(_ source: AudioSource, error: Error, allowRetry: Bool = true) {
        guard !stopped, !stopping else { return }
        sourceGeneration[source] = UUID(); restartGeneration[source] = nil
        let old = inputs.removeValue(forKey: source)
        old?.stop(completion: nil)
        waitingForPermission.remove(source)
        starting.remove(source); active.remove(source); failed.insert(source)
        attempts[source, default: 0] += 1
        messages[source] = "\(label(source)): \(error.localizedDescription)"
        let nsError = error as NSError
        let permissionDenied = nsError.domain == "MeetingMicrophone" && nsError.code == 2
        let canRetry = allowRetry && !permissionDenied && attempts[source, default: 0] <= 5
        if canRetry { nextRetry[source] = clock().addingTimeInterval(Double(min(10, attempts[source, default: 0] * 2))) }
        else { exhausted.insert(source); nextRetry[source] = nil }
        onDiagnostic?(["event": "input_failure", "source": label(source), "retry": canRetry, "reason": error.localizedDescription])
        onStatus?(statusDescription)
        if !initialReported && starting.isEmpty && active.isEmpty && exhausted.isSuperset(of: mode.sources) {
            initialReported = true; completion?(.failure(error)); completion = nil
        }
    }
    func setMicrophoneMuted(_ value: Bool) {
        guard mode.sources.contains(.microphone), !stopped, !stopping else { return }
        muted = value
        mixer.setEnabled(value ? mode.sources.subtracting([.microphone]) : mode.sources)
        if value {
            waitingForPermission.remove(.microphone)
            sourceGeneration[.microphone] = UUID(); restartGeneration[.microphone] = nil
            let old = inputs.removeValue(forKey: .microphone)
            old?.stop(completion: nil); active.remove(.microphone); starting.remove(.microphone); nextRetry[.microphone] = nil
        } else { exhausted.remove(.microphone); attempts[.microphone] = 0; startSource(.microphone) }
        onStatus?(statusDescription)
    }
    private func restart(_ source: AudioSource, reason: String) {
        guard !stopped, !stopping, !(source == .microphone && muted) else { return }
        guard restartGeneration[source] == nil else { return }
        let token = UUID(); restartGeneration[source] = token
        // Retain the old input and callback token through its final drain. A stop
        // during reconnection must still wait for this input before mixer.flush.
        let old = inputs[source]
        active.remove(source); starting.insert(source); startedAt[source] = clock()
        onDiagnostic?(["event": "input_reconnect", "source": label(source), "reason": reason])
        let resume = { [weak self] in
            guard let self, !self.stopped, !self.stopping, self.restartGeneration[source] == token else { return }
            self.startSource(source)
        }
        if let old { old.stop(completion: resume) } else { resume() }
    }
    func tick() {
        guard !stopped, !stopping else { return }
        let now = clock()
        for source in mode.sources where !(source == .microphone && muted) {
            if waitingForPermission.contains(source) {
                if authorization() == .notDetermined { continue }
                waitingForPermission.remove(source)
                startedAt[source] = now
            }
            if starting.contains(source), let started = startedAt[source], now.timeIntervalSince(started) > 30 {
                // Never create repeated concurrent hardware starts after a blocked API call.
                if source == .microphone && authorization() == .notDetermined { continue }
                sourceFailed(source, error: NSError(domain: "Capture", code: 1, userInfo: [NSLocalizedDescriptionKey: "Подключение не ответило. Проверьте разрешение и устройство."]), allowRetry: false)
            } else if active.contains(source), let last = lastCallback[source], now.timeIntervalSince(last) > 8 {
                restart(source, reason: "no_callbacks")
            } else if let due = nextRetry[source], now >= due { startSource(source) }
        }
        guard now.timeIntervalSince(lastPoll) >= 5 else { return }; lastPoll = now
        for source in Array(active) {
            if source == .microphone && selectedMicrophone != nil { continue }
            if let uid = currentUID(source), uid != inputs[source]?.captureUID { restart(source, reason: "default_device_changed") }
        }
    }
    func wake() {
        guard !stopped, !stopping else { return }
        for source in mode.sources where !(source == .microphone && muted) && !exhausted.contains(source) { restart(source, reason: "wake") }
    }
    func stop(completion: (() -> Void)? = nil) {
        guard !stopped else { completion?(); return }
        guard !stopping else { return }
        stopping = true; waitingForPermission.removeAll(); self.completion = nil
        let group = DispatchGroup()
        for input in inputs.values { group.enter(); input.stop { group.leave() } }
        group.notify(queue: .main) { [self] in
            mixer.flush { [self] in
                stopped = true; active.removeAll(); inputs.removeAll(); onSamples = nil
                completion?()
            }
        }
    }
}
