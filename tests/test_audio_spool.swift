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
        chunk.acknowledge()
        try require(!FileManager.default.fileExists(atPath: chunk.path.path), "\(name): ACK did not remove WAV")
        try require(!FileManager.default.fileExists(atPath: chunk.metadata.path), "\(name): ACK did not remove metadata")
    }
    print("PASS \(name): \(chunks.count) chunks")
}

@main private struct AudioSpoolTests {
    static func main() throws {
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
}
