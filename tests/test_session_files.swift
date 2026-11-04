import Foundation
import Darwin

@main struct SessionFilesChecks {
    struct Injected: Error {}
    static func expect(_ value: @autoclosure () -> Bool, _ message: String) {
        guard value() else { fatalError(message) }
    }
    static func read(_ url: URL) throws -> Data { try Data(contentsOf: url) }
    static func files(_ pair: SessionFiles.Pair, session: URL) throws -> (Data, Data, Data) {
        try FileManager.default.createDirectory(at: session, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let audio = Data("synthetic closed WAV; never played".utf8)
        let text = Data("Расшифровка встречи\nАудиозапись: \(pair.audio.path)\nНачало аудиозаписи (00:00:00): anchor\n\n[23:59:59] original text\n".utf8)
        let manifest = try JSONSerialization.data(withJSONObject: ["audio": pair.audio.path, "transcript": pair.transcript.path, "started": pair.startedAt.timeIntervalSince1970, "preserve": 42], options: .sortedKeys)
        try audio.write(to: pair.audio, options: .withoutOverwriting)
        try text.write(to: pair.transcript, options: .withoutOverwriting)
        try manifest.write(to: session.appendingPathComponent("session.json"), options: .withoutOverwriting)
        return (audio, text, manifest)
    }
    static func original(_ pair: SessionFiles.Pair, session: URL, data: (Data, Data, Data)) throws {
        let audio = try read(pair.audio), text = try read(pair.transcript), manifest = try read(session.appendingPathComponent("session.json"))
        expect(audio == data.0 && text == data.1 && manifest == data.2, "rollback did not restore all original bytes")
    }
    static func main() throws {
        if CommandLine.arguments.count == 4 && CommandLine.arguments[1] == "--crash" {
            let directory = URL(fileURLWithPath: CommandLine.arguments[2])
            let pair = try JSONDecoder().decode(SessionFiles.Pair.self, from: read(directory.appendingPathComponent("pair.json")))
            let step = Int(CommandLine.arguments[3])!
            var moves = 0
            _ = try SessionFiles.renameClosedPair(pair, topic: "Итоговая тема", sessionDirectory: directory, move: { from, to in
                try FileManager.default.moveItem(at: from, to: to)
                moves += 1
                if moves == step { _exit(91) }
            })
            fatalError("crash injection point not reached")
        }
        let fm = FileManager.default
        let root = fm.temporaryDirectory.appendingPathComponent("session-files-checks-\(UUID().uuidString)")
        try fm.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: root) }
        let zone = TimeZone(secondsFromGMT: 3 * 3600)!
        let start = ISO8601DateFormatter().date(from: "2026-11-04T20:59:59Z")!
        let first = try SessionFiles.allocate(documentsRoot: root, startedAt: start,
            id: UUID(uuidString: "12345678-0000-0000-0000-000000000001")!, timeZone: zone)
        let second = try SessionFiles.allocate(documentsRoot: root, startedAt: start,
            id: UUID(uuidString: "87654321-0000-0000-0000-000000000001")!, timeZone: zone)
        expect(first.dayDirectory.lastPathComponent == "2026-11-04", "local start day is wrong")
        expect(first.transcript.lastPathComponent == "23-59-59 — Встреча — 12345678.txt", "paired naming contract is wrong")
        expect(first.dayDirectory == second.dayDirectory && first.transcript != second.transcript, "equal-time meetings collide")
        expect(first.audio.deletingPathExtension() == first.transcript.deletingPathExtension(), "audio/TXT stems differ")
        let afterMidnight = try SessionFiles.allocate(documentsRoot: root, startedAt: start.addingTimeInterval(120), timeZone: zone)
        expect(afterMidnight.dayDirectory.lastPathComponent == "2026-11-05", "next local day is not grouped separately")
        for directory in [first.dayDirectory, first.dayDirectory.deletingLastPathComponent()] {
            let mode = try fm.attributesOfItem(atPath: directory.path)[.posixPermissions] as! NSNumber
            expect(mode.intValue == 0o700, "new session directory is not private")
        }
        let session = root.appendingPathComponent("internal", isDirectory: true)
        let bytes = try files(first, session: session)
        _ = try files(second, session: root.appendingPathComponent("other-internal"))
        do { _ = try SessionFiles.allocate(documentsRoot: root, startedAt: start, id: first.id, timeZone: zone); fatalError("existing pair allocation was accepted") }
        catch { }

        for failingMove in [2, 4, 6] {
            var moves = 0
            do {
                _ = try SessionFiles.renameClosedPair(first, topic: "План выпуска", sessionDirectory: session, move: { from, to in
                    moves += 1
                    if moves == failingMove { throw Injected() }
                    try fm.moveItem(at: from, to: to)
                })
                fatalError("injected rename/header/manifest failure ignored")
            } catch { }
            try original(first, session: session, data: bytes)
        }
        print("PASS: failed second rename, header replacement and manifest replacement preserve original pair and manifest")

        try JSONEncoder().encode(first).write(to: session.appendingPathComponent("pair.json"))
        for step in [1, 2, 3, 4, 5, 6] {
            let child = Process()
            child.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
            child.arguments = ["--crash", session.path, String(step)]
            try child.run(); child.waitUntilExit()
            expect(child.terminationStatus == 91, "crash subprocess did not interrupt the intended move")
            try SessionFiles.recover(in: session)
            try original(first, session: session, data: bytes)
            try SessionFiles.recover(in: session)
        }
        print("PASS: actual subprocess exit after each of six transactional moves; recovery restores byte-exact originals and is idempotent")

        let interrupted = Process()
        interrupted.executableURL = URL(fileURLWithPath: CommandLine.arguments[0])
        interrupted.arguments = ["--crash", session.path, "4"]
        try interrupted.run(); interrupted.waitUntilExit()
        let interruptedPair = first.withTopic("Итоговая тема")
        let expectedIntermediate = try read(interruptedPair.transcript)
        let external = Data("independent user edit after crash".utf8)
        try external.write(to: interruptedPair.transcript, options: .atomic)
        do { try SessionFiles.recover(in: session); fatalError("external edit was silently overwritten") }
        catch { }
        let retainedExternal = try read(interruptedPair.transcript)
        expect(retainedExternal == external && fm.fileExists(atPath: interruptedPair.audio.path), "ambiguous recovery altered user data")
        let journals = try fm.contentsOfDirectory(at: session, includingPropertiesForKeys: nil).filter { $0.lastPathComponent.hasPrefix(".pair-rename-") }
        expect(journals.count == 1 && fm.fileExists(atPath: journals[0].appendingPathComponent("original.txt").path), "ambiguous recovery discarded its journal/original copy")
        let backupMode = try fm.attributesOfItem(atPath: journals[0].appendingPathComponent("original.txt").path)[.posixPermissions] as! NSNumber
        expect(backupMode.intValue == 0o600, "transaction backup is not private")
        try expectedIntermediate.write(to: interruptedPair.transcript, options: .atomic)
        try SessionFiles.recover(in: session)
        try original(first, session: session, data: bytes)
        print("PASS: externally changed interrupted TXT and recovery originals remain untouched until conflict is resolved")

        let collision = first.withTopic("Занято")
        try Data("unrelated user file".utf8).write(to: collision.audio)
        do { _ = try SessionFiles.renameClosedPair(first, topic: "Занято", sessionDirectory: session); fatalError("existing destination overwritten") }
        catch { }
        let unrelated = try read(collision.audio)
        expect(unrelated == Data("unrelated user file".utf8), "unrelated target changed")
        try original(first, session: session, data: bytes)

        let longTopic = "../\\:\n\u{0}" + String(repeating: "Очень длинная тема 🧪 ", count: 100)
        let renamed = try SessionFiles.renameClosedPair(first, topic: longTopic, sessionDirectory: session)
        expect(renamed.dayDirectory == first.dayDirectory, "meeting crossing midnight moved out of its start day")
        expect(renamed.audio.deletingLastPathComponent() == first.dayDirectory && renamed.transcript.deletingLastPathComponent() == first.dayDirectory, "unsafe topic escaped the meeting day folder")
        expect(renamed.id == first.id && renamed.audio.lastPathComponent.hasSuffix("12345678.wav"), "stable meeting ID changed at final rename")
        expect(renamed.audio.lastPathComponent.utf8.count <= 255 && renamed.transcript.lastPathComponent.utf8.count <= 255, "Unicode filename exceeds filesystem component limit")
        expect(!renamed.audio.lastPathComponent.contains("..") && !renamed.audio.lastPathComponent.contains("\\") && !renamed.audio.lastPathComponent.contains("\n"), "unsafe topic survived filename sanitation")
        let renamedAudio = try read(renamed.audio)
        expect(renamedAudio == bytes.0, "renaming altered recording bytes")
        let text = try String(contentsOf: renamed.transcript, encoding: .utf8)
        expect(text.contains("Аудиозапись: \(renamed.audio.path)\n") && !text.contains(first.audio.path), "TXT audio reference did not follow pair rename")
        let manifest = try JSONSerialization.jsonObject(with: read(session.appendingPathComponent("session.json"))) as! [String: Any]
        expect(manifest["audio"] as? String == renamed.audio.path && manifest["transcript"] as? String == renamed.transcript.path && manifest["preserve"] as? Int == 42, "manifest paths or unrelated fields changed incorrectly")
        expect(!fm.fileExists(atPath: first.audio.path) && !fm.fileExists(atPath: first.transcript.path), "successful pair rename left stale old paths")
        expect(fm.fileExists(atPath: second.audio.path) && fm.fileExists(atPath: second.transcript.path), "same-day second meeting was altered")
        print("PASS: local date grouping, same-time pairs, private folders, collision refusal, Unicode sanitation and paired final names+references")
    }
}
