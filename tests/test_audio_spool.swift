import Foundation
import AVFoundation

private struct TestFailure: Error, CustomStringConvertible {
    let description: String
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure(description: message) }
}

private func signal(start: Int, count: Int) -> [Float] {
    (start..<(start + count)).map { Float($0 % 10007) / 20000 + 0.1 }
}

private func read(_ chunk: AudioChunk) throws -> (samples: [Float], rate: Double) {
    let file = try AVAudioFile(forReading: chunk.path)
    let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 16_384)!
    var samples = [Float]()
    // AVAudioFile can return a partial read, including on a complete float WAV.
    // Keep reading rather than mistaking an I/O block boundary for lost audio.
    while file.framePosition < file.length {
        try file.read(into: buffer)
        try require(buffer.frameLength > 0, "Unexpected end of WAV before its declared frame count")
        samples.append(contentsOf: UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength)))
    }
    return (samples, file.processingFormat.sampleRate)
}

private func exercise(_ name: String, feed: (AudioSpool) -> Void,
                      verify: ([AudioChunk]) throws -> Void) throws {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("meeting-spool-test-" + UUID().uuidString)
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: directory) }
    let spool = AudioSpool(directory: directory)
    var chunks = [AudioChunk]()
    var failures = [Error]()
    var finished = false
    spool.onChunk = { chunks.append($0) }
    spool.onError = { failures.append($0) }
    feed(spool)
    spool.finish { finished = true }
    let deadline = Date().addingTimeInterval(90)
    while !finished && Date() < deadline {
        RunLoop.main.run(until: Date().addingTimeInterval(0.01))
    }
    try require(finished, "\(name): drain did not finish")
    try require(failures.isEmpty, "\(name): spool errors: \(failures)")
    try require(chunks.map(\.id) == (0..<chunks.count).map { $0 + 1 }, "\(name): chunk IDs are not sequential")
    try verify(chunks)
    // No consumer ran until every input frame was persisted and finish delivered.
    for chunk in chunks {
        try require(FileManager.default.fileExists(atPath: chunk.path.path), "\(name): unacknowledged WAV missing")
        let record = try JSONSerialization.jsonObject(with: Data(contentsOf: chunk.metadata)) as! [String: Any]
        try require(record["request_id"] as? Int == chunk.id, "\(name): metadata ID mismatch")
        try require(record["timestamp"] as? Double == chunk.timestamp, "\(name): metadata timestamp mismatch")
        let mode = try FileManager.default.attributesOfItem(atPath: chunk.path.path)[.posixPermissions] as! NSNumber
        try require(mode.intValue == 0o600, "\(name): WAV permissions must be private")
        try chunk.acknowledge()
        try require(!FileManager.default.fileExists(atPath: chunk.path.path), "\(name): ACK did not remove WAV")
        try require(!FileManager.default.fileExists(atPath: chunk.metadata.path), "\(name): ACK did not remove metadata")
    }
    print("PASS \(name): \(chunks.count) chunks")
}

@main private struct AudioSpoolTests {
    static func main() {
        do { try run() }
        catch {
            FileHandle.standardError.write(Data("FAIL AudioSpool: \(error)\n".utf8))
            exit(1)
        }
    }

    private static func run() throws {
        try checkCleanupFailureAndRetry(permissions: 0o500)
        try checkCleanupFailureAndRetry(permissions: 0o000)
        try checkSidecarOnlyCleanup()
        try checkPermanentRecording()
        let base = 1_700_000_000.0
        let rate = 48_000.0
        let total = Int(rate * 600.125)
        try exercise("ten minutes with stalled consumer and partial final chunk", feed: { spool in
            var position = 0
            while position < total {
                let count = min(12_000, total - position)
                spool.add(signal(start: position, count: count), rate: rate,
                          timestamp: base + Double(position + count) / rate)
                position += count
            }
        }, verify: { chunks in
            var previousEnd = 0
            for (index, chunk) in chunks.enumerated() {
                let audio = try read(chunk)
                try require(audio.rate == rate, "long run: unexpected sample rate")
                let end = Int(((chunk.timestamp - base) * rate).rounded())
                let start = end - audio.samples.count
                try require(start == (index == 0 ? 0 : previousEnd - Int(rate * 0.7)), "long run: gap or wrong overlap at chunk \(chunk.id); start=\(start), previousEnd=\(previousEnd), end=\(end), frames=\(audio.samples.count), timestamp=\(chunk.timestamp)")
                for (offset, value) in audio.samples.enumerated() {
                    let expected = Float((start + offset) % 10007) / 20000 + 0.1
                    try require(value == expected, "long run: altered/missing sample at \(start + offset)")
                }
                previousEnd = end
            }
            try require(previousEnd == total, "long run: final samples lost")
            try require(chunks.last!.duration < 6, "long run: expected partial final chunk")
        })

        try exercise("short session tail", feed: { spool in
            spool.add(signal(start: 0, count: 180_000), rate: rate, timestamp: base + 3.75)
        }, verify: { chunks in
            try require(chunks.count == 1, "short tail: expected one chunk")
            let audio = try read(chunks[0])
            try require(audio.samples.count == 180_000, "short tail: wrong frame count")
            try require(chunks[0].timestamp == base + 3.75, "short tail: timestamp changed during drain")
        })

        try exercise("sample-rate change flushes both tails", feed: { spool in
            spool.add(signal(start: 0, count: 150_000), rate: rate, timestamp: base + 3.125)
            spool.add(signal(start: 150_000, count: 110_250), rate: 44_100, timestamp: base + 5.625)
        }, verify: { chunks in
            try require(chunks.count == 2, "rate change: expected two chunks")
            let first = try read(chunks[0]), second = try read(chunks[1])
            try require(first.rate == rate && first.samples.count == 150_000, "rate change: first tail altered")
            try require(second.rate == 44_100 && second.samples.count == 110_250, "rate change: second tail altered")
            try require(chunks.map(\.timestamp) == [base + 3.125, base + 5.625], "rate change: capture timestamps changed")
            try require(first.samples == signal(start: 0, count: 150_000), "rate change: first signal corrupted")
            try require(second.samples == signal(start: 150_000, count: 110_250), "rate change: second signal corrupted")
        })

        try exercise("oversized callback uses capture timestamps", feed: { spool in
            spool.add(signal(start: 0, count: 224_000), rate: 16_000, timestamp: base + 14)
        }, verify: { chunks in
            try require(chunks.count == 3, "large callback: expected two full chunks and tail")
            for (chunk, expected) in zip(chunks, [base + 6, base + 11.3, base + 14]) {
                try require(abs(chunk.timestamp - expected) < 0.000001, "large callback: timestamp drift")
            }
            let last = try read(chunks[2])
            try require(last.samples.count == 54_400, "large callback: final tail length mismatch")
        })

        try exercise("exact chunk boundary does not duplicate overlap", feed: { spool in
            spool.add(signal(start: 0, count: 288_000), rate: rate, timestamp: base + 6)
        }, verify: { chunks in
            try require(chunks.count == 1, "exact boundary: overlap was emitted twice")
        })
        print("PASS all 5 AudioSpool tests")
    }

    private static func checkCleanupFailureAndRetry(permissions: Int) throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("meeting-ack-test-" + UUID().uuidString)
        let fm = FileManager.default
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer {
            try? fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
            try? fm.removeItem(at: directory)
        }
        let chunk = AudioChunk(id: 42, path: directory.appendingPathComponent("chunk-00000042.wav"), timestamp: 123.5, duration: 6)
        try Data("artificial input, never played".utf8).write(to: chunk.path)
        try JSONSerialization.data(withJSONObject: chunk.request).write(to: chunk.metadata)
        try fm.setAttributes([.posixPermissions: permissions], ofItemAtPath: directory.path)
        var failure: Error?
        do { try chunk.acknowledge() } catch { failure = error }
        try fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        try require(failure != nil, "ACK cleanup must report a denied WAV deletion before the next request can advance")
        try require(fm.fileExists(atPath: chunk.path.path), "Failed ACK cleanup removed the recoverable WAV")
        try require(fm.fileExists(atPath: chunk.metadata.path), "Failed ACK cleanup removed request identity")
        try chunk.acknowledge()
        try require(!fm.fileExists(atPath: chunk.path.path), "Retry did not remove committed WAV")
        try require(!fm.fileExists(atPath: chunk.metadata.path), "Retry did not remove committed metadata")
        try chunk.acknowledge()
        print("PASS ACK cleanup permissions \(String(permissions, radix: 8)), restored-permission retry and idempotence")
    }

    private static func checkSidecarOnlyCleanup() throws {
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("meeting-ack-sidecar-" + UUID().uuidString)
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        let chunk = AudioChunk(id: 3, path: directory.appendingPathComponent("chunk-00000003.wav"), timestamp: 40, duration: 6)
        try JSONSerialization.data(withJSONObject: chunk.request).write(to: chunk.metadata)
        try chunk.acknowledge()
        try require(!fm.fileExists(atPath: chunk.metadata.path), "Sidecar-only retry did not finish cleanup")
        print("PASS ACK cleanup of sidecar after WAV already removed")
    }

    private static func checkPermanentRecording() throws {
        let fm = FileManager.default
        let directory = fm.temporaryDirectory.appendingPathComponent("meeting-recording-spool-" + UUID().uuidString)
        let queue = directory.appendingPathComponent("queue")
        try fm.createDirectory(at: queue, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: directory) }
        let archive = directory.appendingPathComponent("permanent.wav")
        let recording = try MeetingRecording(path: archive, startedAt: 100)
        let spool = AudioSpool(directory: queue, recording: recording)
        var chunks = [AudioChunk](), failures = [Error](), finished = false
        spool.onChunk = { chunks.append($0) }; spool.onError = { failures.append($0) }
        spool.add([Float](repeating: 0.25, count: 288_000), rate: 48_000, timestamp: 106)
        spool.add([Float](repeating: 0, count: 288_000), rate: 48_000, timestamp: 112)
        spool.add([Float](repeating: -0.5, count: 54_000), rate: 48_000, timestamp: 113.125)
        spool.finish { finished = true }
        let deadline = Date().addingTimeInterval(10)
        while !finished && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        try require(finished && failures.isEmpty && !chunks.isEmpty, "Recording/spool did not drain successfully")
        for chunk in chunks { try chunk.acknowledge() }
        let permanent = try read(AudioChunk(id: 0, path: archive, timestamp: 113.125, duration: 13.125))
        try require(permanent.samples.count == 630_000, "ASR overlap/silence filtering altered permanent recording duration")
        try require(permanent.samples[0..<288_000].allSatisfy { $0 == 0.25 }, "Permanent first signal corrupted")
        try require(permanent.samples[288_000..<576_000].allSatisfy { $0 == 0 }, "Permanent silence removed")
        try require(permanent.samples[576_000...].allSatisfy { $0 == -0.5 }, "Permanent partial tail lost")
        let remaining = try fm.contentsOfDirectory(at: queue, includingPropertiesForKeys: nil)
        try require(remaining.isEmpty, "ACK left temporary audio")
        print("PASS permanent recording survives all ASR ACKs, keeps silence and tail without overlap duplication")
    }
}
