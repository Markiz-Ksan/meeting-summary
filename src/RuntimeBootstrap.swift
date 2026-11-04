import Foundation
import Darwin

protocol RuntimeBootstrapChild: AnyObject {
    var isRunning: Bool { get }
    var terminationStatus: Int32 { get }
    func run() throws
    func readAvailableData() -> Data
    func waitUntilExit()
    func cancel()
}

/// Serial setup jobs share one child. Cancellation invalidates the operation
/// before signalling it; queued starts, UI progress and success cannot revive it.
final class RuntimeBootstrapCoordinator {
    private final class Operation {
        var child: RuntimeBootstrapChild?
        var observers: [(progress: (String) -> Void, completion: (Result<Void, Error>) -> Void)] = []
    }
    private let queue: DispatchQueue
    private let lock = NSLock()
    private let validate: () throws -> Void
    private let makeChild: () throws -> RuntimeBootstrapChild
    private var operation: Operation?

    init(queue: DispatchQueue = DispatchQueue(label: "local.meeting.runtime-setup", qos: .utility),
         validate: @escaping () throws -> Void = {},
         makeChild: @escaping () throws -> RuntimeBootstrapChild) {
        self.queue = queue; self.validate = validate; self.makeChild = makeChild
    }

    func prepare(progress: @escaping (String) -> Void,
                 completion: @escaping (Result<Void, Error>) -> Void) {
        lock.lock()
        if let current = operation {
            current.observers.append((progress, completion))
            lock.unlock(); return
        }
        let current = Operation()
        current.observers.append((progress, completion))
        operation = current
        lock.unlock()
        queue.async { self.perform(current) }
    }

    func cancel() {
        lock.lock()
        guard let current = operation else { lock.unlock(); return }
        operation = nil
        let child = current.child
        let observers = current.observers
        current.observers.removeAll()
        lock.unlock()
        child?.cancel() // Signals only; never waits for child exit on the caller's queue.
        let error = NSError(domain: "MeetingRuntimeSetup", code: NSUserCancelledError,
                            userInfo: [NSLocalizedDescriptionKey: "Подготовка отменена"])
        DispatchQueue.main.async { observers.forEach { $0.completion(.failure(error)) } }
    }

    private func perform(_ current: Operation) {
        lock.lock(); let active = operation === current; lock.unlock()
        guard active else { return }
        do {
            try validate()
            let child = try makeChild()
            // Check, publish and launch form one critical section. cancel() can
            // either invalidate the queued launch or signal the launched child.
            lock.lock()
            guard operation === current else { lock.unlock(); return }
            current.child = child
            do { try child.run() }
            catch { lock.unlock(); throw error }
            lock.unlock()
            var buffer = Data()
            var ready = false
            var lastError = "Не удалось подготовить локальные модели. Проверьте сеть и свободное место, затем повторите запуск."
            while true {
                let bytes = child.readAvailableData()
                if bytes.isEmpty { break }
                buffer.append(bytes)
                while let newline = buffer.firstIndex(of: 10) {
                    let line = buffer.prefix(upTo: newline)
                    buffer.removeSubrange(...newline)
                    if let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] {
                        if event["event"] as? String == "ready" { ready = true }
                        if let text = event["text"] as? String {
                            if event["event"] as? String == "error" { lastError = text }
                            publish(text, current: current)
                        }
                    }
                }
                if buffer.count > 1024 * 1024 { buffer.removeAll(keepingCapacity: true) }
            }
            child.waitUntilExit()
            guard child.terminationStatus == 0 && ready else { throw RuntimeBootstrap.failure(lastError) }
            finish(current, result: .success(()))
        } catch { finish(current, result: .failure(error)) }
    }

    private func publish(_ text: String, current: Operation) {
        DispatchQueue.main.async {
            self.lock.lock()
            let observers = self.operation === current ? current.observers : []
            self.lock.unlock()
            observers.forEach { $0.progress(text) }
        }
    }
    private func finish(_ current: Operation, result: Result<Void, Error>) {
        DispatchQueue.main.async {
            self.lock.lock()
            guard self.operation === current else { self.lock.unlock(); return }
            let observers = current.observers
            current.observers.removeAll()
            self.operation = nil
            self.lock.unlock()
            observers.forEach { $0.completion(result) }
        }
    }
}

private final class RuntimeBootstrapProcess: RuntimeBootstrapChild {
    private let child = Process()
    private let output = Pipe()
    var isRunning: Bool { child.isRunning }
    var terminationStatus: Int32 { child.terminationStatus }
    init() {
        child.executableURL = AppPaths.python
        child.arguments = ["-I", "-B", "-u", AppPaths.resources.appendingPathComponent("bootstrap.py").path,
                           "--root", AppPaths.root.path, "--profile", AppPaths.modelProfile]
        var environment = AppPaths.workerEnvironment
        environment.removeValue(forKey: "HF_HUB_OFFLINE")
        environment.removeValue(forKey: "TRANSFORMERS_OFFLINE")
        child.environment = environment
        child.standardOutput = output; child.standardError = output
    }
    func run() throws { try child.run() }
    func readAvailableData() -> Data { output.fileHandleForReading.availableData }
    func waitUntilExit() { child.waitUntilExit() }
    func cancel() {
        guard child.isRunning else { return }
        child.terminate()
        let retained = child, pid = child.processIdentifier
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 3) {
            if retained.isRunning, retained.processIdentifier == pid { _ = Darwin.kill(pid, SIGKILL) }
        }
    }
}

/// Downloads public pinned assets before capture is enabled. Never reads audio.
final class RuntimeBootstrap {
    private static let coordinator = RuntimeBootstrapCoordinator(validate: {
        #if !arch(arm64)
        throw failure("Приложение поддерживает только Mac с Apple Silicon")
        #endif
        guard ProcessInfo.processInfo.isOperatingSystemAtLeast(
            OperatingSystemVersion(majorVersion: 14, minorVersion: 4, patchVersion: 0)) else {
            throw failure("Требуется macOS 14.4 или новее")
        }
        guard ProcessInfo.processInfo.physicalMemory >= 8 * 1024 * 1024 * 1024 else {
            throw failure("Требуется не менее 8 ГБ объединённой памяти.")
        }
    }, makeChild: { RuntimeBootstrapProcess() })

    static func prepare(progress: @escaping (String) -> Void,
                        completion: @escaping (Result<Void, Error>) -> Void) {
        guard AppPaths.isPortableBundle else {
            DispatchQueue.main.async { completion(.success(())) }; return
        }
        coordinator.prepare(progress: progress, completion: completion)
    }
    static func cancel() { coordinator.cancel() }
    fileprivate static func failure(_ message: String) -> NSError {
        NSError(domain: "MeetingRuntimeSetup", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
