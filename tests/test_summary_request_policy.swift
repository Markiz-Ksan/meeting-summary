import Foundation

@main struct SummaryRequestChecks {
    static var failures = 0
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() { print("FAIL: \(message)"); failures += 1 }
    }
    static func allows(_ text: String = "Анна пришлёт итоговую смету в среду.",
                       source: Double = 1000, now: Double = 1001,
                       previous: Double = 980, lastText: String = "") -> Bool {
        SummaryRequestPolicy.allows(text: text, sourceTimestamp: source, now: now,
                                   lastRequested: previous, lastSubmittedText: lastText)
    }
    static func main() {
        let shortPhrases = ["Встреча в пятницу.", "Релиз отменён.", "Анна согласилась."]
        for phrase in shortPhrases {
            expect(allows(phrase), "Meaningful short speech is silently suppressed: \(phrase)")
        }
        expect(allows(), "Ordinary changed source suppressed")
        expect(!allows(" \n\t "), "Blank source scheduled")
        expect(!allows("Релиз отменён.", lastText: "Релиз отменён."), "Unchanged context resubmitted")
        expect(!allows(now: 1001, previous: 990), "Twelve-second throttle skipped")
        expect(allows(now: 1002, previous: 990), "Boundary update suppressed")
        expect(!allows(source: 701, now: 1001), "Expired context resubmitted")
        expect(allows(source: 702, now: 1001), "Fresh five-minute boundary suppressed")
        expect(!allows(source: 1040, now: 1001), "Far-future context treated as fresh")
        expect(allows(source: 1001.5, now: 1001), "Minor timestamp jitter suppressed")
        expect(allows(now: 1001, previous: 1060), "Wall-clock rollback blocks changed current speech")
        for invalid in [Double.nan, Double.infinity, -Double.infinity] {
            expect(!allows(source: invalid), "Invalid source timestamp accepted")
            expect(!allows(now: invalid), "Invalid current clock accepted")
            expect(!allows(previous: invalid), "Invalid request timestamp accepted")
        }
        if failures > 0 { exit(1) }
        print("PASS: short phrases, whitespace, deduplication, throttle, expiry, future timestamps and clock rollback; no audio")
    }
}
