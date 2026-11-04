import Foundation

private final class FakeBootstrapChild: RuntimeBootstrapChild {
    private let condition = NSCondition()
    private var running = false
    private var closed = false
    private var data = Data()
    private(set) var runs = 0
    private(set) var cancellations = 0
    let waited = DispatchSemaphore(value: 0)
    var isRunning: Bool { condition.lock(); defer { condition.unlock() }; return running }
    var terminationStatus: Int32 { 0 }
    func run() throws { condition.lock(); runs += 1; running = true; condition.broadcast(); condition.unlock() }
    func readAvailableData() -> Data {
        condition.lock(); defer { condition.unlock() }
        while data.isEmpty && !closed { condition.wait() }
        let result = data; data.removeAll(); return result
    }
    func waitUntilExit() { waited.signal() }
    func cancel() {
        condition.lock(); cancellations += 1; running = false; closed = true
        condition.broadcast(); condition.unlock()
    }
    func finish() {
        condition.lock(); data = Data("{\"event\":\"progress\",\"text\":\"fixture\"}\n{\"event\":\"ready\"}\n".utf8)
        closed = true; running = false; condition.broadcast(); condition.unlock()
    }
}

@main struct RuntimeBootstrapChecks {
    static func expect(_ value: @autoclosure () -> Bool, _ message: String) {
        if !value() { fatalError(message) }
    }
    static func pump(_ predicate: () -> Bool, timeout: TimeInterval = 2) {
        let until = Date().addingTimeInterval(timeout)
        while !predicate() && Date() < until { RunLoop.current.run(until: Date().addingTimeInterval(0.005)) }
        expect(predicate(), "asynchronous setup did not complete")
    }
    static func main() {
        do {
            let queue = DispatchQueue(label: "test.runtime.queued"); queue.suspend()
            let child = FakeBootstrapChild()
            let coordinator = RuntimeBootstrapCoordinator(queue: queue, makeChild: { child })
            var calls = 0
            coordinator.prepare(progress: { _ in fatalError("cancelled queued progress escaped") }) { result in
                calls += 1
                if case .success = result { fatalError("cancelled queued setup succeeded") }
            }
            coordinator.cancel(); queue.resume()
            pump { calls == 1 }
            expect(child.runs == 0 && child.cancellations == 0, "cancelled queued child launched")
        }
        do {
            let child = FakeBootstrapChild()
            let coordinator = RuntimeBootstrapCoordinator(makeChild: { child })
            var calls = 0
            for _ in 0..<2 {
                coordinator.prepare(progress: { _ in }) { result in
                    if case .failure(let error) = result { fatalError("deduplicated setup failed: \(error)") }
                    calls += 1
                }
            }
            pump { child.isRunning }
            child.finish(); pump { calls == 2 }
            expect(child.runs == 1, "duplicate prepare launched more than one process")
        }
        do {
            let child = FakeBootstrapChild()
            let replacement = FakeBootstrapChild()
            var made = 0
            let coordinator = RuntimeBootstrapCoordinator(makeChild: {
                made += 1; return made == 1 ? child : replacement
            })
            var cancelled = 0; var succeeded = 0
            coordinator.prepare(progress: { _ in fatalError("cancelled running progress escaped") }) { result in
                if case .success = result { fatalError("cancelled running setup succeeded") }
                cancelled += 1
            }
            pump { child.isRunning }
            coordinator.cancel()
            coordinator.prepare(progress: { _ in }) { result in
                if case .failure = result { fatalError("fresh setup inherited cancellation") }
                succeeded += 1
            }
            pump { cancelled == 1 && replacement.isRunning }
            replacement.finish(); pump { succeeded == 1 }
            expect(child.cancellations == 1 && child.runs == 1 && replacement.runs == 1,
                   "cancel/retry lost a callback or leaked a child")
        }
        do {
            let child = FakeBootstrapChild()
            let coordinator = RuntimeBootstrapCoordinator(makeChild: { child })
            var calls = 0
            coordinator.prepare(progress: { _ in fatalError("stale progress escaped after completed-child cancellation") }) { result in
                if case .success = result { fatalError("queued success escaped cancellation") }
                calls += 1
            }
            pump { child.isRunning }
            child.finish()
            expect(child.waited.wait(timeout: .now()+2) == .success, "fixture child did not exit")
            // Main callbacks have not been pumped since finish: cancellation must
            // replace the queued success and suppress queued progress exactly once.
            coordinator.cancel(); pump { calls == 1 }
        }
        print("PASS: queued cancellation, one-process deduplication, running cancellation, exactly-once callbacks fresh retry and stale-success suppression")
    }
}
