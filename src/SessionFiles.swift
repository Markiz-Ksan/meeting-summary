import Foundation
import Darwin

/// Session file operations run serially, only after both writers have closed.
/// Files outside this session's explicitly recorded pair are never migrated.
enum SessionFiles {
    struct Pair: Codable {
        let dayDirectory: URL
        let transcript: URL
        let audio: URL
        let startedAt: Date
        let id: UUID
        let clock: String

        func withTopic(_ topic: String) -> Pair {
            let stem = "\(clock) — \(SessionFiles.safeTopic(topic)) — \(id.uuidString.lowercased().prefix(8))"
            return Pair(dayDirectory: dayDirectory,
                        transcript: dayDirectory.appendingPathComponent(stem + ".txt"),
                        audio: dayDirectory.appendingPathComponent(stem + ".wav"),
                        startedAt: startedAt, id: id, clock: clock)
        }
    }
    private struct Journal: Codable {
        let old: Pair
        let new: Pair
        let audioIdentity: String
    }
    typealias Move = (URL, URL) throws -> Void
    private static let fm = FileManager.default

    static func allocate(documentsRoot: URL = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0],
                         startedAt: Date = Date(), id: UUID = UUID(), title: String = "Встреча",
                         timeZone: TimeZone = .current) throws -> Pair {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        let folder = documentsRoot.appendingPathComponent("Сводка встреч", isDirectory: true)
        try privateDirectory(folder)
        let day = folder.appendingPathComponent(formatter.string(from: startedAt), isDirectory: true)
        try privateDirectory(day)
        formatter.dateFormat = "HH-mm-ss"
        let pair = Pair(dayDirectory: day, transcript: day, audio: day, startedAt: startedAt,
                        id: id, clock: formatter.string(from: startedAt)).withTopic(title)
        try requireAbsent(pair.audio); try requireAbsent(pair.transcript)
        return pair
    }

    static func safeTopic(_ topic: String) -> String {
        let scalars = topic.precomposedStringWithCanonicalMapping.unicodeScalars.map {
            CharacterSet.controlCharacters.contains($0) || "/\\:.".unicodeScalars.contains($0) ? " " : String($0)
        }.joined()
        let clean = scalars.components(separatedBy: .whitespacesAndNewlines).filter { !$0.isEmpty }.joined(separator: " ")
        var result = ""
        for character in clean {
            if result.utf8.count + String(character).utf8.count > 200 { break }
            result.append(character)
        }
        return result.isEmpty ? "Встреча" : result.trimmingCharacters(in: .whitespaces)
    }

    /// Journal and original bytes are durable before the first move. The caller
    /// must close TXT and WAV first; path changes cannot retarget an open writer.
    static func renameClosedPair(_ pair: Pair, topic: String, sessionDirectory: URL,
                                 move: Move = { try FileManager.default.moveItem(at: $0, to: $1) }) throws -> Pair {
        try recover(in: sessionDirectory)
        let destination = pair.withTopic(topic)
        if destination.audio == pair.audio { return pair }
        try requireAbsent(destination.audio); try requireAbsent(destination.transcript)
        _ = try identity(pair.transcript)
        let transcript = try Data(contentsOf: pair.transcript)
        let manifestURL = sessionDirectory.appendingPathComponent("session.json")
        _ = try identity(manifestURL)
        let manifest = try Data(contentsOf: manifestURL)
        let updatedText = try updatedTranscript(transcript, from: pair.audio, to: destination.audio)
        let updatedJSON = try updatedManifest(manifest, old: pair, new: destination)
        let journal = Journal(old: pair, new: destination, audioIdentity: try identity(pair.audio))
        var dayInfo = stat(), sessionInfo = stat()
        guard lstat(pair.dayDirectory.path, &dayInfo) == 0,
              lstat(sessionDirectory.path, &sessionInfo) == 0,
              dayInfo.st_dev == sessionInfo.st_dev else { throw failure("Папки записи и сеанса должны быть на одном томе") }
        let transaction = sessionDirectory.appendingPathComponent(".pair-rename-\(UUID().uuidString)", isDirectory: true)
        try privateDirectory(transaction)
        // A crash before journal.json is written has made no external changes;
        // incomplete preparation is retained, never treated as user-file cleanup.
        try write(transcript, transaction.appendingPathComponent("original.txt"))
        try write(manifest, transaction.appendingPathComponent("original-session.json"))
        try write(updatedText, transaction.appendingPathComponent("replacement.txt"))
        try write(updatedJSON, transaction.appendingPathComponent("replacement-session.json"))
        try write(JSONEncoder().encode(journal), transaction.appendingPathComponent("journal.json"))
        try syncDirectory(transaction); try syncDirectory(sessionDirectory)
        do {
            try move(pair.audio, destination.audio); try syncDirectory(pair.dayDirectory)
            try move(pair.transcript, destination.transcript); try syncDirectory(pair.dayDirectory)
            try move(destination.transcript, transaction.appendingPathComponent("held.txt"))
            try move(transaction.appendingPathComponent("replacement.txt"), destination.transcript)
            try syncDirectory(pair.dayDirectory); try syncDirectory(transaction)
            try move(manifestURL, transaction.appendingPathComponent("held-session.json"))
            try move(transaction.appendingPathComponent("replacement-session.json"), manifestURL)
            try syncDirectory(sessionDirectory); try syncDirectory(transaction)
            try recoverTransaction(transaction, sessionDirectory: sessionDirectory, commit: true)
        } catch {
            do { try recoverTransaction(transaction, sessionDirectory: sessionDirectory) }
            catch let rollbackError {
                throw failure("Переименование прервано. Исходники и журнал сохранены; восстановление: \(rollbackError.localizedDescription)")
            }
            throw error
        }
        return destination
    }

    /// Invoke for every retained internal session before any capture/worker opens
    /// it. Interrupted moves roll back; a durable commit keeps the new names.
    static func recover(in sessionDirectory: URL) throws {
        for directory in try fm.contentsOfDirectory(at: sessionDirectory, includingPropertiesForKeys: nil)
            where directory.lastPathComponent.hasPrefix(".pair-rename-") {
            if fm.fileExists(atPath: directory.appendingPathComponent("journal.json").path) {
                try recoverTransaction(directory, sessionDirectory: sessionDirectory)
            }
        }
    }

    private static func recoverTransaction(_ directory: URL, sessionDirectory: URL, commit: Bool = false) throws {
        var directoryInfo = stat()
        guard lstat(directory.path, &directoryInfo) == 0,
              directoryInfo.st_mode & S_IFMT == S_IFDIR else { throw failure("Каталог журнала недоступен или заменён") }
        let journalData = try Data(contentsOf: directory.appendingPathComponent("journal.json"))
        let journal = try JSONDecoder().decode(Journal.self, from: journalData)
        let old = journal.old, new = journal.new
        guard old.dayDirectory == new.dayDirectory, old.audio.deletingLastPathComponent() == old.dayDirectory,
              new.audio.deletingLastPathComponent() == old.dayDirectory,
              old.transcript.deletingLastPathComponent() == old.dayDirectory,
              new.transcript.deletingLastPathComponent() == old.dayDirectory else { throw failure("Некорректные пути журнала") }
        let text = try Data(contentsOf: directory.appendingPathComponent("original.txt"))
        let manifest = try Data(contentsOf: directory.appendingPathComponent("original-session.json"))
        let updatedText = try updatedTranscript(text, from: old.audio, to: new.audio)
        let updatedJSON = try updatedManifest(manifest, old: old, new: new)
        let manifestURL = sessionDirectory.appendingPathComponent("session.json")
        let oldAudio = fm.fileExists(atPath: old.audio.path), newAudio = fm.fileExists(atPath: new.audio.path)
        guard oldAudio != newAudio, try identity(oldAudio ? old.audio : new.audio) == journal.audioIdentity else {
            throw failure("Аудиофайл изменён или пути заняты; журнал сохранён")
        }
        let oldText = fm.fileExists(atPath: old.transcript.path), newText = fm.fileExists(atPath: new.transcript.path)
        guard !(oldText && newText) else { throw failure("Пути расшифровки заняты; журнал сохранён") }
        let textLocation = oldText ? old.transcript : (newText ? new.transcript : nil)
        if let location = textLocation { try requireKnown(location, choices: [text, updatedText]) }
        if fm.fileExists(atPath: manifestURL.path) { try requireKnown(manifestURL, choices: [manifest, updatedJSON]) }
        let known: [String: [Data]] = ["journal.json": [journalData], "original.txt": [text],
            "original-session.json": [manifest], "replacement.txt": [updatedText],
            "replacement-session.json": [updatedJSON], "held.txt": [text],
            "held-session.json": [manifest], "rollback.txt": [text, updatedText],
            "rollback-session.json": [manifest, updatedJSON], "rollback.txt.restore": [text],
            "rollback-session.json.restore": [manifest]]
        // Refuse unknown or externally changed transaction contents before any rollback.
        let entries = try fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        for entry in entries {
            guard let choices = known[entry.lastPathComponent] else { throw failure("Неизвестный файл в журнале; сохранён") }
            if entry.lastPathComponent.hasSuffix(".restore") {
                let partial = try Data(contentsOf: entry)
                guard choices[0].starts(with: partial) else { throw failure("Временный файл восстановления изменён; сохранён") }
            } else { try requireKnown(entry, choices: choices) }
        }
        if commit {
            guard newAudio, newText, try Data(contentsOf: new.transcript) == updatedText,
                  try Data(contentsOf: manifestURL) == updatedJSON else { throw failure("Завершённая пара изменена; резервные копии сохранены") }
        } else {
            if newAudio { try fm.moveItem(at: new.audio, to: old.audio) }
            try restore(text, at: old.transcript, current: textLocation, held: directory.appendingPathComponent("rollback.txt"))
            try restore(manifest, at: manifestURL, current: fm.fileExists(atPath: manifestURL.path) ? manifestURL : nil,
                        held: directory.appendingPathComponent("rollback-session.json"))
            try syncDirectory(old.dayDirectory); try syncDirectory(sessionDirectory)
        }
        // Retire the whole journal atomically before cleanup. A power loss during
        // deletion must never leave an active journal with half its backups gone.
        let retired = sessionDirectory.appendingPathComponent(".completed-rename-\(UUID().uuidString)")
        try fm.moveItem(at: directory, to: retired)
        // Retirement is the commit point. Cleanup cannot turn a committed pair
        // into an error carrying stale old paths; leftovers remain recoverable.
        try? syncDirectory(sessionDirectory)
        for name in known.keys {
            let file = retired.appendingPathComponent(name)
            if fm.fileExists(atPath: file.path), (try? requireKnown(file, choices: known[name]!)) != nil {
                try? fm.removeItem(at: file)
            }
        }
        _ = Darwin.rmdir(retired.path)
        try? syncDirectory(sessionDirectory)
    }

    private static func restore(_ original: Data, at target: URL, current: URL?, held: URL) throws {
        if let current {
            if current == target, try Data(contentsOf: current) == original { return }
            if current != target, try Data(contentsOf: current) == original {
                try fm.moveItem(at: current, to: target); return
            }
            try fm.moveItem(at: current, to: held)
        }
        // Never expose a partially written original if disk space runs out
        // during rollback. The next recovery can retry this private staging file.
        let staged = held.appendingPathExtension("restore")
        if fm.fileExists(atPath: staged.path) {
            guard original.starts(with: try Data(contentsOf: staged)) else { throw failure("Файл восстановления изменён; сохранён") }
            try fm.removeItem(at: staged)
        }
        try write(original, staged)
        try fm.moveItem(at: staged, to: target)
    }
    private static func updatedTranscript(_ data: Data, from old: URL, to new: URL) throws -> Data {
        guard let text = String(data: data, encoding: .utf8) else { throw failure("Расшифровка не в UTF-8") }
        let lines = text.components(separatedBy: "\n")
        guard lines.filter({ $0.hasPrefix("Аудиозапись:") }).count == 1,
              lines.contains("Аудиозапись: \(old.path)") else { throw failure("Ссылка на аудио в расшифровке не совпадает") }
        return Data(lines.map { $0 == "Аудиозапись: \(old.path)" ? "Аудиозапись: \(new.path)" : $0 }.joined(separator: "\n").utf8)
    }
    private static func updatedManifest(_ data: Data, old: Pair, new: Pair) throws -> Data {
        guard var object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              object["audio"] as? String == old.audio.path,
              object["transcript"] as? String == old.transcript.path else { throw failure("Пути манифеста не совпадают") }
        object["audio"] = new.audio.path; object["transcript"] = new.transcript.path
        return try JSONSerialization.data(withJSONObject: object, options: .sortedKeys)
    }
    private static func requireKnown(_ url: URL, choices: [Data]) throws {
        _ = try identity(url)
        guard choices.contains(try Data(contentsOf: url)) else { throw failure("Файл изменён извне; сохранён: \(url.lastPathComponent)") }
    }
    private static func requireAbsent(_ url: URL) throws {
        var info = stat()
        guard lstat(url.path, &info) != 0, errno == ENOENT else { throw failure("Путь уже занят: \(url.lastPathComponent)") }
    }
    private static func identity(_ url: URL) throws -> String {
        var info = stat()
        guard lstat(url.path, &info) == 0, info.st_mode & S_IFMT == S_IFREG else { throw failure("Не найден обычный файл: \(url.lastPathComponent)") }
        return "\(info.st_dev):\(info.st_ino)"
    }
    private static func privateDirectory(_ url: URL) throws {
        var info = stat()
        if lstat(url.path, &info) == 0 {
            guard info.st_mode & S_IFMT == S_IFDIR else { throw failure("Путь каталога занят") }
        } else {
            guard mkdir(url.path, 0o700) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        }
    }
    private static func write(_ data: Data, _ url: URL) throws {
        let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        try handle.write(contentsOf: data); try handle.synchronize()
    }
    private static func syncDirectory(_ url: URL) throws {
        let fd = open(url.path, O_RDONLY)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { close(fd) }
        guard fsync(fd) == 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
    }
    private static func failure(_ message: String) -> NSError {
        NSError(domain: "SessionFiles", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
