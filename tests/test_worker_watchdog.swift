import Foundation

@main struct WorkerWatchdogChecks {
    static var failures = 0
    static func expect(_ value: @autoclosure () -> Bool, _ message: String) {
        if !value() { print("FAIL: \(message)"); failures += 1 }
    }
    static func main() {
        // A startup hang cannot consume the whole 120-second recovery allowance.
        var startup = WorkerWatchdog()
        startup.launched(at: 100)
        expect(startup.poll(at: 189.99) == .none, "startup timed out early")
        expect(startup.poll(at: 190) == .fail(.startup), "startup hang not detected with recovery time left")
        expect(startup.failed(at: 190) == .none, "first failure exhausted retries")
        expect(startup.poll(at: 190.99) == .none, "retry delay skipped")
        expect(startup.poll(at: 191) == .restart, "replacement not scheduled")
        expect(startup.poll(at: 191) == .none, "replacement scheduled twice")
        startup.launched(at: 191)
        expect(startup.ready(at: 210), "replacement ready was rejected")
        expect(startup.beginRequest(id: 1, at: 211), "replay request was rejected")
        expect(startup.progress(id: 1, at: 215), "matching replay start missing")
        expect(startup.poll(at: 220) == .exhausted(.recoveryDeadline), "startup/replay refreshed the hard recovery budget")

        // A healthy idle child must not reload models during a long silence.
        var idle = WorkerWatchdog()
        idle.launched(at: 10)
        expect(idle.ready(at: 11), "initial ready rejected")
        expect(!idle.ready(at: 1_000), "duplicate ready treated as progress")
        expect(idle.poll(at: 10_000) == .none, "silence restarted a healthy idle worker")
        expect(idle.beginRequest(id: 7, at: 10_000), "request after silence rejected")
        expect(idle.poll(at: 10_044.9) == .none, "idle time charged to new request")
        expect(idle.poll(at: 10_045) == .fail(.request), "hung request ignored")

        // Only the first start for the active ID counts; stale IDs, diagnostics,
        // and repeated heartbeats must never keep an artificial hang alive.
        var requests = WorkerWatchdog()
        requests.launched(at: 0)
        _ = requests.ready(at: 1)
        _ = requests.beginRequest(id: 42, at: 2)
        expect(!requests.progress(id: 41, at: 40), "stale progress reset deadline")
        expect(!requests.complete(id: 41, at: 41), "stale result cleared active request")
        expect(requests.progress(id: 42, at: 3), "matching start rejected")
        expect(!requests.progress(id: 42, at: 44), "duplicate heartbeat reset deadline")
        expect(requests.poll(at: 48) == .fail(.request), "duplicate/stale messages disabled timeout")
        _ = requests.failed(at: 48)
        expect(requests.poll(at: 49) == .restart, "first replacement missing")
        requests.launched(at: 49)
        _ = requests.ready(at: 50)
        _ = requests.beginRequest(id: 42, at: 51)
        _ = requests.progress(id: 42, at: 52)
        expect(requests.poll(at: 123) == .exhausted(.recoveryDeadline), "replay granted a new 120-second allowance")

        // Ready alone cannot reset the crash loop; a committed matching result can.
        var crashes = WorkerWatchdog()
        crashes.launched(at: 0)
        for attempt in 0..<3 {
            expect(crashes.failed(at: Double(attempt * 3 + 1)) == .none, "bounded retry ended too early")
            expect(crashes.poll(at: Double(attempt * 3 + 2)) == .restart, "crash retry absent")
            crashes.launched(at: Double(attempt * 3 + 2))
            _ = crashes.ready(at: Double(attempt * 3 + 3))
        }
        expect(crashes.failed(at: 11) == .exhausted(.restartLimit), "ready messages defeated restart limit")
        expect(crashes.poll(at: 500) == .none, "exhausted worker retried")
        crashes.reset()
        crashes.launched(at: 600)
        _ = crashes.ready(at: 601)
        _ = crashes.beginRequest(id: 4, at: 602)
        _ = crashes.failed(at: 603)
        _ = crashes.poll(at: 604)
        crashes.launched(at: 604)
        _ = crashes.ready(at: 605)
        _ = crashes.beginRequest(id: 4, at: 606)
        expect(crashes.complete(id: 4, at: 607), "replayed completion rejected")
        expect(crashes.poll(at: 1_000) == .none, "successful completion left recovery deadline armed")
        expect(crashes.failed(at: 1_001) == .none, "success did not reset crash budget")
        crashes.stop()
        expect(crashes.poll(at: 10_000) == .none, "stop left a queued restart")
        expect(!crashes.ready(at: 10_000), "late ready escaped stop")
        expect(!crashes.complete(id: 4, at: 10_000), "late result escaped stop")

        // A backwards wall clock is irrelevant because only monotonic inputs are used.
        expect(MonotonicTime.now > 0, "monotonic source invalid")
        if failures > 0 { exit(1) }
        print("PASS: bounded startup/request/recovery, idle silence, correlated progress, crash budget, completion reset and stop")
    }
}
