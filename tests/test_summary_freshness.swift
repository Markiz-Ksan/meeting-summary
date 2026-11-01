import Foundation

@main struct FreshnessChecks {
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        if !condition() { fatalError(message) }
    }
    static func date(_ seconds: Double) -> Date { Date(timeIntervalSince1970: seconds) }
    static func main() {
        var state = SummaryFreshness()
        expect(state.labels(now: date(1000)).summary.contains("не сформирована"), "Empty state claims a live summary")
        state.recordSpeech(at: date(1000))
        state.publish(source: date(1000), displayed: date(1090))
        var labels = state.labels(now: date(1092))
        expect(labels.summary.contains("1 мин 32 с"), "Late result presents old source as fresh")
        expect(labels.summary.contains("показана 2 с назад"), "Display age missing")
        state.recordSpeech(at: date(1100))
        labels = state.labels(now: date(1105))
        expect(labels.speech.contains("5 с назад"), "Latest speech not tracked independently")
        expect(labels.summary.contains("1 мин 45 с"), "New ASR speech reset displayed summary age")
        state.reject()
        labels = state.labels(now: date(1110))
        expect(labels.summary.contains("предыдущая"), "Rejected draft presented as new")
        expect(labels.summary.contains("1 мин 50 с"), "Rejection reset the old summary timestamp")
        state.publish(source: date(1100), displayed: date(1111))
        expect(!state.labels(now: date(1112)).summary.contains("предыдущая"), "Accepted result did not clear rejected state")
        state.clearDisplayed()
        expect(state.labels(now: date(1113)).summary.contains("не сформирована"), "Cleared overlay retains freshness claim")
        expect(state.labels(now: date(1113)).speech.contains("13 с назад"), "Clearing overlay erased last speech")
        state.publish(source: date(1100), displayed: date(1111))
        expect(state.labels(now: date(900)).summary.contains("часы"), "Clock rollback made old content appear current")
        let preview = state.labels(now: date(1114), preview: true)
        expect(preview.summary.contains("Пример") && !preview.summary.contains("назад"), "Synthetic preview claims live timestamps")
        state.reset()
        expect(state.labels(now: date(1200)).speech.contains("не получена"), "New session inherits previous speech")
        expect(state.labels(now: date(1200)).summary.contains("не сформирована"), "New session inherits old summary")
        print("PASS: source/display age, independent ASR, rejection, acceptance, clearing, rollback, preview and reset; no audio")
    }
}
