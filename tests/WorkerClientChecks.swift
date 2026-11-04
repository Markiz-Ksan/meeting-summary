import Foundation
import Darwin

@main
struct WorkerClientChecks {
    static func pump(until condition: () -> Bool, timeout: TimeInterval = 10) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition(), Date() < deadline {
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
        return condition()
    }

    static func expect(_ value: @autoclosure () -> Bool, _ message: String) {
        guard value() else { fatalError(message) }
    }

    static func main() throws {
        let directory = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let fixture = directory.appendingPathComponent("tests/fixtures/worker_client_fixture.py")
        let session = FileManager.default.temporaryDirectory.appendingPathComponent("worker-client-checks-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: session) }
        func client(resume: Bool = false, audio: URL? = nil) -> WorkerClient {
            WorkerClient(mode: "transcribe", session: session, transcript: audio == nil ? nil : session.appendingPathComponent("transcript.txt"), resume: resume,
                         audioFile: audio, audioStartedAt: audio == nil ? nil : 123.5,
                         executableURL: URL(fileURLWithPath: "/usr/bin/python3"), scriptURL: fixture)
        }

        let audio = session.appendingPathComponent("Встреча с пробелами.wav")
        let worker = client(audio: audio)
        var events: [[String: Any]] = []
        var status: Int32?
        var finalDeliveredBeforeExit = false
        var callbacksOnMain = true
        worker.onEvent = { event in
            callbacksOnMain = callbacksOnMain && Thread.isMainThread
            events.append(event)
        }
        worker.onExit = { result in
            callbacksOnMain = callbacksOnMain && Thread.isMainThread
            finalDeliveredBeforeExit = events.contains { $0["event"] as? String == "final" }
            status = result
        }
        try worker.start()
        expect(pump(until: { events.contains { $0["event"] as? String == "ready" } }), "ready was not delivered")
        let ready = events.first { $0["event"] as? String == "ready" }
        expect(ready?["audio_file"] as? String == audio.path && ready?["audio_start"] as? Double == 123.5,
               "audio path/start metadata did not reach the child intact")
        expect(events.filter { $0["event"] as? String == "protocol_error" }.count == 2,
               "malformed and oversized stdout must each report a bounded protocol error")
        let diagnostics = events.filter { ["protocol_error", "diagnostic"].contains($0["event"] as? String ?? "") }
        let diagnosticJSON = String(data: try JSONSerialization.data(withJSONObject: diagnostics), encoding: .utf8)!
        expect(!diagnosticJSON.contains("PRIVATE_DO_NOT_EXPOSE"), "diagnostics exposed raw child text")
        expect(diagnostics.filter { $0["kind"] as? String == "worker_stderr" }
            .allSatisfy { ($0["tailBytes"] as? Int ?? 0) <= 8192 }, "stderr tail exceeded 8 KiB")

        let started = Date()
        try worker.send(["id": 1, "text": String(repeating: "a", count: 1024 * 1024)])
        expect(Date().timeIntervalSince(started) < 1, "large stdin request blocked the caller")
        expect(pump(until: { events.contains { $0["event"] as? String == "result" && $0["id"] as? Int == 1 } }), "large request was not fully delivered")
        expect(events.first { $0["event"] as? String == "result" }?["length"] as? Int == 1024 * 1024, "partial stdin request")
        do {
            try worker.send(["text": String(repeating: "a", count: 2 * 1024 * 1024)])
            fatalError("oversized stdin request was accepted")
        } catch { }
        try worker.send(["op": "exit", "id": 2])
        expect(pump(until: { status != nil }), "exit callback was not delivered")
        expect(status == 7, "wrong child exit status")
        expect(finalDeliveredBeforeExit, "exit callback arrived before the final stdout event")
        expect(events.contains { $0["event"] as? String == "final" && $0["id"] as? Int == 2 }, "final stdout was lost at exit")
        expect(callbacksOnMain, "worker callbacks did not run on main")
        expect(!worker.isRunning, "exited child remains running")
        worker.stop()
        print("PASS: protocol bounds/privacy, main callbacks, 1 MiB nonblocking stdin, final event before exit")

        let brokenPipe = client()
        var pipeEvents: [String] = []
        var pipePID: Int32 = 0
        brokenPipe.onEvent = {
            pipeEvents.append($0["event"] as? String ?? "")
            if let pid = $0["pid"] as? Int { pipePID = Int32(pid) }
        }
        try brokenPipe.start()
        expect(pump(until: { pipeEvents.contains("ready") }), "EPIPE fixture not ready")
        try brokenPipe.send(["op": "close_stdin"])
        expect(pump(until: { pipeEvents.contains("stdin_closed") }), "fixture did not close stdin")
        do {
            try brokenPipe.send(["id": 3])
            fatalError("closed stdin did not throw")
        } catch { }
        brokenPipe.stop()
        expect(pump(until: { Darwin.kill(pipePID, 0) == -1 && errno == ESRCH }, timeout: 5), "EPIPE worker leaked")
        print("PASS: EPIPE is caught without SIGPIPE; child is reaped")

        let stubborn = client(resume: true)
        var stubbornPID: Int32 = 0
        var postStopCallbacks = 0
        var stopped = false
        stubborn.onEvent = {
            if stopped { postStopCallbacks += 1 }
            if let pid = $0["pid"] as? Int { stubbornPID = Int32(pid) }
        }
        stubborn.onExit = { _ in if stopped { postStopCallbacks += 1 } }
        try stubborn.start()
        expect(pump(until: { stubbornPID > 0 }), "stubborn worker not ready")
        stopped = true
        let stopTime = Date()
        stubborn.stop()
        expect(Date().timeIntervalSince(stopTime) < 1, "stop blocked on child shutdown")
        let replacement = client()
        var replacementPID: Int32 = 0
        var replacementReplied = false
        replacement.onEvent = {
            if let pid = $0["pid"] as? Int { replacementPID = Int32(pid) }
            if $0["event"] as? String == "result" { replacementReplied = true }
        }
        try replacement.start()
        expect(pump(until: { replacementPID > 0 }), "replacement worker not ready")
        expect(pump(until: { Darwin.kill(stubbornPID, 0) == -1 && errno == ESRCH }, timeout: 6), "SIGTERM-ignoring child leaked")
        expect(Date().timeIntervalSince(stopTime) >= 2.5, "stubborn fixture did not exercise delayed SIGKILL")
        expect(postStopCallbacks == 0, "callbacks escaped stop invalidation")
        expect(replacement.isRunning, "old kill timer terminated the replacement worker")
        try replacement.send(["id": 4])
        expect(pump(until: { replacementReplied }), "replacement did not survive old worker shutdown")
        replacement.stop()
        expect(pump(until: { Darwin.kill(replacementPID, 0) == -1 && errno == ESRCH }, timeout: 5), "replacement worker leaked")
        print("PASS: delayed SIGKILL removes only the retained child; no callbacks after stop")
    }
}
