import Foundation
import AVFoundation
import Darwin

private struct Failure: Error, CustomStringConvertible { let description: String }
private func require(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
    if try !condition() { throw Failure(description: message) }
}
private func samples(_ path: URL) throws -> [Float] {
    let file = try AVAudioFile(forReading: path)
    try require(file.fileFormat.sampleRate == 48_000, "Unexpected archive sample rate")
    let buffer = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 480_000)!
    try file.read(into: buffer)
    return Array(UnsafeBufferPointer(start: buffer.floatChannelData![0], count: Int(buffer.frameLength)))
}

@main private struct RecordingChecks {
    static func main() {
        do { try run() }
        catch { FileHandle.standardError.write(Data("FAIL recording: \(error)\n".utf8)); exit(1) }
    }
    private static func run() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("meeting-recording-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("meeting.wav")
        let recording = try MeetingRecording(path: path, startedAt: 1_700_000_000)
        try recording.append([Float](repeating: 0.25, count: 48_000), rate: 48_000, timestamp: 1_700_000_001)
        try require(try samples(path).count == 48_000, "WAV unreadable while recording is open")
        try recording.append([Float](repeating: 0, count: 48_000), rate: 48_000, timestamp: 1_700_000_002)
        try recording.append([Float](repeating: -0.5, count: 24_000), rate: 48_000, timestamp: 1_700_000_003.5)
        try recording.finish()
        try recording.finish()
        let result = try samples(path)
        try require(result.count == 168_000, "Silence, timestamp gap or final partial batch was lost")
        try require(result[0] == 0.25 && result[47_999] == 0.25, "Signal samples changed")
        try require(result[48_000..<144_000].allSatisfy { $0 == 0 }, "Silence or source gap not retained")
        try require(result[144_000...].allSatisfy { $0 == -0.5 }, "Final signal lost")
        let mode = try FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions] as! NSNumber
        try require(mode.intValue == 0o600, "Recording is not private")
        do { _ = try MeetingRecording(path: path, startedAt: 0); throw Failure(description: "Existing recording overwritten") }
        catch is Failure { throw Failure(description: "Existing recording overwritten") }
        catch { }
        try require(try samples(path).count == 168_000, "Exclusive open changed an existing recording")
        print("PASS recording: live readability, all samples, silence/gaps, tail, private exclusive file")

        let overlapPath = directory.appendingPathComponent("overlap.wav")
        let overlap = try MeetingRecording(path: overlapPath, startedAt: 100)
        try overlap.append([Float](repeating: 0.25, count: 48_000), rate: 48_000, timestamp: 101)
        try overlap.append([Float](repeating: -0.5, count: 48_000), rate: 48_000, timestamp: 101.5)
        try overlap.finish()
        let overlapSamples = try samples(overlapPath)
        try require(overlapSamples.count == 72_000, "Overlapping callbacks duplicated audio")
        try require(overlapSamples[0] == 0.25 && overlapSamples[48_000] == -0.5, "Overlap trimmed the wrong samples")
        print("PASS recording: callback overlap is not recorded twice")

        let rejectedPath = directory.appendingPathComponent("invalid.wav")
        let rejected = try MeetingRecording(path: rejectedPath, startedAt: 100)
        try rejected.append([Float](repeating: 0.25, count: 480), rate: 48_000, timestamp: 100.01)
        do { try rejected.append([0.5], rate: 44_100, timestamp: 101); throw Failure(description: "Unexpected sample rate accepted") }
        catch is Failure { throw Failure(description: "Unexpected sample rate accepted") }
        catch { }
        try rejected.finish()
        try require(try samples(rejectedPath) == [Float](repeating: 0.25, count: 480), "Rejected batch changed the recording")
        print("PASS recording: incompatible input does not corrupt saved prefix")

        let limitedPath = directory.appendingPathComponent("write-error.wav")
        let limited = try MeetingRecording(path: limitedPath, startedAt: 100)
        try limited.append([Float](repeating: 0.25, count: 32), rate: 48_000, timestamp: 100 + 32.0/48_000)
        var prior = rlimit()
        try require(getrlimit(RLIMIT_FSIZE, &prior) == 0, "Cannot inspect process file limit")
        let oldSignal = signal(SIGXFSZ, SIG_IGN)
        var cap = rlimit(rlim_cur: 128, rlim_max: prior.rlim_max)
        try require(setrlimit(RLIMIT_FSIZE, &cap) == 0, "Cannot constrain synthetic write")
        var writeFailed = false
        do { try limited.append([Float](repeating: -0.5, count: 32), rate: 48_000, timestamp: 100 + 64.0/48_000) }
        catch { writeFailed = true }
        let restored = setrlimit(RLIMIT_FSIZE, &prior)
        signal(SIGXFSZ, oldSignal)
        try require(restored == 0, "Cannot restore process file limit")
        try require(writeFailed, "Real filesystem failure was not reported")
        try limited.finish()
        try require(try samples(limitedPath) == [Float](repeating: 0.25, count: 32), "Failed write corrupted the prior committed audio")
        print("PASS recording: real write failure is reported and prior prefix remains readable")

        if let target = ProcessInfo.processInfo.environment["MEETING_RECORDING_EVIDENCE"] {
            let destination = URL(fileURLWithPath: target, isDirectory: true)
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
            for file in [path, overlapPath, limitedPath] {
                try FileManager.default.copyItem(at: file, to: destination.appendingPathComponent(file.lastPathComponent))
            }
        }
    }
}
