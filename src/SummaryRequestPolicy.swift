import Foundation

enum SummaryRequestPolicy {
    static func allows(text: String, sourceTimestamp: TimeInterval, now: TimeInterval,
                       lastRequested: TimeInterval, lastSubmittedText: String) -> Bool {
        guard now.isFinite, sourceTimestamp.isFinite, lastRequested.isFinite,
              text != lastSubmittedText,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        let age = now - sourceTimestamp
        let elapsed = now - lastRequested
        guard age.isFinite, elapsed.isFinite, age >= -1, age < 300 else { return false }
        // A corrected wall clock must not postpone new speech until the old
        // clock catches up. The caller records this request using the new time.
        return elapsed < 0 || elapsed >= 12
    }
}
