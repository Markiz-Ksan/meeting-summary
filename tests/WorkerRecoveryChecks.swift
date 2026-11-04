import Foundation
import Darwin

@main struct WorkerRecoveryChecks {
    static func expect(_ value: @autoclosure () -> Bool, _ message: String) {
        guard value() else { fatalError(message) }
    }
    static func pump(_ condition: () -> Bool, timeout: TimeInterval = 7) -> Bool {
        let deadline = MonotonicTime.now + timeout
        while !condition(), MonotonicTime.now < deadline {
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        return condition()
    }
    static func integers(_ url: URL) -> [Int] {
        (try? String(contentsOf: url, encoding: .utf8))?.split(separator: "\n").compactMap { Int($0) } ?? []
    }
    static func dead(_ pid: Int) -> Bool { Darwin.kill(Int32(pid), 0) == -1 && errno == ESRCH }

    static func run(mode: String, scenario: String, python: URL, fixture: URL) throws {
        let session = FileManager.default.temporaryDirectory.appendingPathComponent("worker-recovery-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: session) }
        try JSONSerialization.data(withJSONObject: ["scenario": scenario]).write(to: session.appendingPathComponent("fixture.json"))
        var pending = [1, 2]
        for id in pending { try Data("retained synthetic payload \(id)".utf8).write(to: session.appendingPathComponent("queue-\(id).json")) }
        let began = MonotonicTime.now
        // A controllable clock compresses the policy's 45/90/120-second deadlines.
        // Child creation, pipes, OS signals, callbacks and disk queue are real.
        func now() -> TimeInterval { (MonotonicTime.now - began) * 25 }
        var watchdog = WorkerWatchdog()
        var worker: WorkerClient?
        var inFlight: Int?
        var completed = [Int]()
        var callbacksAfterStop = 0
        var stopped = false
        var failures = 0
        var restarts = 0
        var firstFailureAt: TimeInterval?
        var restoredAt: TimeInterval?
        var retainedAtFailure = false
        var terminal: WorkerWatchdog.Failure?
        var recoveryAnchor = now()

        func sendNext() {
            guard inFlight == nil, let id = pending.first, let child = worker,
                  watchdog.beginRequest(id: id, at: now()) else { return }
            inFlight = id
            if failures == 0 { recoveryAnchor = now() }
            do { try child.send(["request_id": id, "text": "synthetic"]) }
            catch { fatalError("synthetic send failed: \(error)") }
        }
        func fail() {
            failures += 1
            if firstFailureAt == nil { firstFailureAt = now() }
            retainedAtFailure = pending == [1, 2] && pending.allSatisfy {
                FileManager.default.fileExists(atPath: session.appendingPathComponent("queue-\($0).json").path)
            }
            worker?.stop(); worker = nil; inFlight = nil
            if case .exhausted(let why) = watchdog.failed(at: now()) { terminal = why }
        }
        func launch() throws {
            let child = WorkerClient(mode: mode, session: session, transcript: nil,
                                     executableURL: python, scriptURL: fixture)
            worker = child
            child.onEvent = { [weak child] event in
                if stopped { callbacksAfterStop += 1; return }
                guard let child, worker === child else { return }
                switch event["event"] as? String {
                case "ready": if watchdog.ready(at: now()) { sendNext() }
                case "progress":
                    if let id = event["request_id"] as? Int,
                       watchdog.progress(id: id, at: now()), failures == 0 { recoveryAnchor = now() }
                case "result":
                    guard let id = event["request_id"] as? Int, id == inFlight,
                          watchdog.complete(id: id, at: now()) else { return }
                    if firstFailureAt != nil && restoredAt == nil { restoredAt = now() }
                    completed.append(id)
                    try! FileManager.default.removeItem(at: session.appendingPathComponent("queue-\(id).json"))
                    pending.removeAll { $0 == id }; inFlight = nil
                    sendNext()
                default: break
                }
            }
            child.onExit = { [weak child] _ in
                if stopped { callbacksAfterStop += 1; return }
                guard let child, worker === child else { return }
                fail()
            }
            watchdog.launched(at: now())
            try child.start()
        }
        defer { worker?.stop() }
        try launch()
        let finished = pump({
            switch watchdog.poll(at: now()) {
            case .none: break
            case .fail: fail()
            case .restart: restarts += 1; try! launch()
            case .exhausted(let why): terminal = why
            }
            return pending.isEmpty || terminal != nil
        }, timeout: 9)
        expect(finished, "\(mode)/\(scenario): policy never reached recovery or preservation")
        let permanent = scenario == "permanent_hang"
        expect(permanent ? terminal == .recoveryDeadline : terminal == nil,
               "\(mode)/\(scenario): wrong recovery outcome: \(String(describing: terminal))")
        expect(permanent ? (failures >= 1 && restarts >= 1 && restarts <= 3) : (failures == 1 && restarts == 1),
               "\(mode)/\(scenario): wrong number of bounded recovery attempts")
        expect(retainedAtFailure, "\(mode)/\(scenario): unacknowledged queue removed on failure")
        expect(completed == (permanent ? [] : [1, 2]), "\(mode)/\(scenario): replay lost, duplicated or reordered a completion")
        let delivery = integers(session.appendingPathComponent("received.txt"))
        let expectedDelivery = scenario == "startup_hang" ? [1, 2] : [1, 1, 2]
        expect(permanent ? (!delivery.isEmpty && delivery.allSatisfy { $0 == 1 } && delivery.count <= restarts+1)
                         : delivery == expectedDelivery,
               "\(mode)/\(scenario): failed request was not replayed intact")
        expect(permanent ? restoredAt == nil : (restoredAt != nil && restoredAt!-recoveryAnchor < 120),
               "\(mode)/\(scenario): recovery did not fit the synthetic 120-second budget")
        let files = try FileManager.default.contentsOfDirectory(atPath: session.path)
        expect(permanent ? (pending == [1, 2] && files.filter { $0.hasPrefix("queue-") }.count == 2)
                         : (pending.isEmpty && !files.contains { $0.hasPrefix("queue-") }),
               "\(mode)/\(scenario): completed queue was not drained")
        stopped = true
        watchdog.stop()
        worker?.stop(); worker = nil
        expect(pump({ integers(session.appendingPathComponent("pids.txt")).allSatisfy(dead) }),
               "\(mode)/\(scenario): an old or replacement subprocess leaked")
        expect(callbacksAfterStop == 0, "\(mode)/\(scenario): stopped generation delivered callbacks")
        expect(watchdog.poll(at: 1_000) == .none, "\(mode)/\(scenario): stop scheduled a late restart")
        let outcome = permanent ? "120s budget exhausted; both queue files retained"
                                : "queue replay+drain; restored at \(String(format: "%.1f", restoredAt!)) synthetic seconds"
        print("PASS: \(mode)/\(scenario), real child/pipes/signals, \(outcome), all children reaped")
    }

    static func main() throws {
        let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let fixture = root.appendingPathComponent("tests/fixtures/recovery_worker.py")
        let python = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/MeetingOverlay/runtime/.venv/bin/python")
        expect(FileManager.default.isExecutableFile(atPath: python.path), "deployed Python runtime missing")
        try run(mode: "transcribe", scenario: "request_hang", python: python, fixture: fixture)
        try run(mode: "summarize", scenario: "request_hang", python: python, fixture: fixture)
        try run(mode: "summarize", scenario: "startup_hang", python: python, fixture: fixture)
        try run(mode: "transcribe", scenario: "permanent_hang", python: python, fixture: fixture)
    }
}
