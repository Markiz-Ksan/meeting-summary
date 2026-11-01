import Foundation

struct SummaryFreshness {
    private var latestSpeech: Date?
    private var source: Date?
    private var displayed: Date?
    private var rejected = false
    mutating func recordSpeech(at date: Date) { latestSpeech = date }
    mutating func publish(source: Date, displayed: Date) { self.source = source; self.displayed = displayed; rejected = false }
    mutating func reject() { rejected = true }
    mutating func clearDisplayed() { source = nil; displayed = nil; rejected = false }
    mutating func reset() { self = SummaryFreshness() }
    private static let clockFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .none
        formatter.timeStyle = .medium
        return formatter
    }()
    private func age(_ date: Date, now: Date) -> String {
        let interval = now.timeIntervalSince(date)
        guard interval.isFinite, interval >= -1 else { return "часы изменились" }
        guard interval < 365 * 86400 else { return "больше года назад" }
        let seconds = Int(max(0, interval))
        if seconds < 60 { return "\(seconds) с назад" }
        if seconds < 3600 { return "\(seconds / 60) мин \(seconds % 60) с назад" }
        return "\(seconds / 3600) ч \((seconds % 3600) / 60) мин назад"
    }
    func labels(now: Date, preview: Bool = false) -> (speech: String, summary: String) {
        if preview { return ("Пример · реальная речь не используется", "Пример сводки · без времени реальной встречи") }
        let speech = latestSpeech.map {
            "Последняя речь: \(Self.clockFormatter.string(from: $0)) · \(age($0, now: now))"
        } ?? "Последняя речь: ещё не получена"
        guard let source, let displayed else { return (speech, "Сводка: ещё не сформирована") }
        let prefix = rejected ? "Показана предыдущая сводка" : "Сводка"
        return (speech, "\(prefix): речь \(Self.clockFormatter.string(from: source)) · \(age(source, now: now)); показана \(age(displayed, now: now))")
    }
}
