import Foundation
import Darwin

/// Permanent recording of the mixer timeline, before ASR overlap/silence filtering.
/// Owned by AudioSpool's serial queue. No capture, playback or model dependency.
final class MeetingRecording {
    let path: URL
    let startedAt: Double
    private var handle: FileHandle?
    private var frames: Int64 = 0
    private let sampleRate = 48_000.0
    private let maximumFrames = Int64((UInt32.max - 36) / 2)

    init(path: URL, startedAt: Double) throws {
        guard startedAt.isFinite else { throw CocoaError(.fileWriteInvalidFileName) }
        self.path = path; self.startedAt = startedAt
        let descriptor = open(path.path, O_RDWR | O_CREAT | O_EXCL, 0o600)
        guard descriptor >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        handle = FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
        do { try writeHeader() }
        catch { try? handle?.close(); handle = nil; throw error }
    }

    deinit { try? finish() }

    func append(_ values: [Float], rate: Double, timestamp: Double) throws {
        guard let handle else { throw CocoaError(.fileWriteUnknown) }
        guard rate == sampleRate, timestamp.isFinite else { throw CocoaError(.fileWriteInapplicableStringEncoding) }
        guard !values.isEmpty else { return }
        let end = ((timestamp - startedAt) * sampleRate).rounded()
        guard end.isFinite, end <= Double(maximumFrames) else {
            throw NSError(domain: "MeetingRecording", code: 1, userInfo: [NSLocalizedDescriptionKey: "Достигнут предел WAV-записи (около 12 часов). Сохранённая часть оставлена в Документах."])
        }
        if end <= Double(frames) { return }
        let endFrame = Int64(end)
        let startFrame = endFrame - Int64(values.count)
        let skip = Int(max(0, frames - startFrame))
        guard skip < values.count else { return }
        // Seeking creates zero-filled sparse gaps: timestamps stay aligned even
        // if a source disappears or the mixer skips a long sleep interval.
        let firstFrame = max(frames, startFrame)
        try handle.seek(toOffset: UInt64(44 + firstFrame * 2))
        var pcm = Data(capacity: (values.count - skip) * 2)
        for value in values[skip...] {
            let bounded = value.isFinite ? min(1, max(-1, value)) : 0
            let quantized = Int16(max(-32768, min(32767, Int(bounded * 32768))))
            var encoded = quantized.littleEndian
            withUnsafeBytes(of: &encoded) { pcm.append(contentsOf: $0) }
        }
        try handle.write(contentsOf: pcm)
        frames = endFrame
        try writeHeader()
    }

    func finish() throws {
        guard let handle else { return }
        defer { try? handle.close(); self.handle = nil }
        try writeHeader()
        try handle.synchronize()
    }

    private func writeHeader() throws {
        guard let handle else { throw CocoaError(.fileWriteUnknown) }
        let bytes = UInt32(frames * 2)
        var header = Data()
        func word<T: FixedWidthInteger>(_ number: T) {
            var encoded = number.littleEndian
            withUnsafeBytes(of: &encoded) { header.append(contentsOf: $0) }
        }
        header.append(contentsOf: "RIFF".utf8); word(bytes + 36)
        header.append(contentsOf: "WAVEfmt ".utf8); word(UInt32(16))
        word(UInt16(1)); word(UInt16(1)); word(UInt32(48_000)); word(UInt32(96_000))
        word(UInt16(2)); word(UInt16(16))
        header.append(contentsOf: "data".utf8); word(bytes)
        try handle.seek(toOffset: 0)
        try handle.write(contentsOf: header)
    }
}
