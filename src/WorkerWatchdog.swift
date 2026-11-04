import Foundation

/// Deadlines use elapsed machine time; changing the calendar cannot postpone recovery.
enum MonotonicTime {
    static var now: TimeInterval { ProcessInfo.processInfo.systemUptime }
}

/// Main-queue policy for one worker. It never owns or acknowledges queued data.
/// `ready` is startup progress, not proof that a replayed request has completed.
struct WorkerWatchdog {
    enum Failure: String {
        case startup, request, recoveryDeadline, restartLimit
    }
    enum Action: Equatable {
        case none
        case fail(Failure)
        case restart
        case exhausted(Failure)
    }
    private enum Phase {
        case stopped, starting, idle, request(Int), waiting, launchRequested, exhausted
    }
    private var phase = Phase.stopped
    private var lastProgress: TimeInterval = 0
    private var acceptedStart = false
    private var recoveryAnchor: TimeInterval?
    private var retryAt: TimeInterval = 0
    private var restarts = 0

    // Detection leaves time for process teardown, model loading and replay.
    // Actual model recovery within the remaining budget requires separate measurement.
    private let startupLimit: TimeInterval = 90
    private let requestLimit: TimeInterval = 45
    private let recoveryLimit: TimeInterval = 120
    private let retryDelay: TimeInterval = 1
    private let restartLimit = 3

    mutating func reset() { self = WorkerWatchdog() }
    mutating func stop() { phase = .stopped; recoveryAnchor = nil }

    mutating func launched(at now: TimeInterval) {
        phase = .starting
        lastProgress = now
        acceptedStart = false
    }

    @discardableResult
    mutating func ready(at now: TimeInterval) -> Bool {
        guard case .starting = phase, !recoveryExpired(at: now) else { return false }
        phase = .idle
        // A ready replacement still owes the request that caused recovery.
        return true
    }

    @discardableResult
    mutating func beginRequest(id: Int, at now: TimeInterval) -> Bool {
        guard case .idle = phase, !recoveryExpired(at: now) else { return false }
        phase = .request(id)
        lastProgress = now
        acceptedStart = false
        return true
    }

    /// The wire protocol emits one `progress` when it accepts a request. Repeated
    /// starts/heartbeats and progress for other IDs are not advancing work.
    @discardableResult
    mutating func progress(id: Int, at now: TimeInterval) -> Bool {
        guard case .request(let active) = phase, active == id, !acceptedStart,
              !recoveryExpired(at: now) else { return false }
        acceptedStart = true
        lastProgress = now
        return true
    }

    @discardableResult
    mutating func complete(id: Int, at now: TimeInterval) -> Bool {
        guard case .request(let active) = phase, active == id,
              !recoveryExpired(at: now) else { return false }
        phase = .idle
        lastProgress = now
        recoveryAnchor = nil
        restarts = 0
        return true
    }

    /// Caller stops the old child and retains its unacknowledged request. The
    /// budget is frozen at last progress, not renewed by each failed replacement.
    mutating func failed(at now: TimeInterval) -> Action {
        switch phase {
        case .stopped, .exhausted, .waiting: return .none
        default: break
        }
        if recoveryAnchor == nil {
            switch phase {
            case .starting, .request: recoveryAnchor = lastProgress
            default: recoveryAnchor = now
            }
        }
        if recoveryExpired(at: now) { return exhaust(.recoveryDeadline) }
        guard restarts < restartLimit else { return exhaust(.restartLimit) }
        restarts += 1
        retryAt = now + retryDelay
        phase = .waiting
        return .none
    }

    mutating func poll(at now: TimeInterval) -> Action {
        switch phase {
        case .stopped, .exhausted: return .none
        default: break
        }
        if recoveryExpired(at: now) { return exhaust(.recoveryDeadline) }
        switch phase {
        case .starting where now - lastProgress >= startupLimit: return .fail(.startup)
        case .request where now - lastProgress >= requestLimit: return .fail(.request)
        case .waiting where now >= retryAt:
            phase = .launchRequested
            return .restart
        default: return .none
        }
    }

    private func recoveryExpired(at now: TimeInterval) -> Bool {
        recoveryAnchor.map { now - $0 >= recoveryLimit } ?? false
    }
    private mutating func exhaust(_ reason: Failure) -> Action {
        phase = .exhausted
        return .exhausted(reason)
    }
}
