import Foundation
import AVFoundation

struct AudioChunk {
    let id: Int
    let path: URL
    let timestamp: Double
    let duration: Double
    var metadata: URL { path.deletingPathExtension().appendingPathExtension("json") }
    var request: [String: Any] { ["path": path.path, "timestamp": timestamp, "request_id": id] }
    func acknowledge() throws {
        do {
            // Do not use fileExists: an inaccessible directory can look absent.
            try FileManager.default.removeItem(at: path)
        } catch let error as NSError where error.domain == NSCocoaErrorDomain && error.code == NSFileNoSuchFileError {
            // Idempotent after a successful prior cleanup.
        }
        // Only a remaining WAV can be replayed. A sidecar alone is harmless;
        // keep it for a later cleanup if its own deletion is denied.
        try? FileManager.default.removeItem(at: metadata)
    }
}

/// A serial disk spool, independent of inference speed. Only the current six seconds stay in RAM.
final class AudioSpool {
    let directory: URL
    let queue = DispatchQueue(label: "local.meeting.spool", qos: .userInitiated)
    var onChunk: ((AudioChunk) -> Void)?
    var onError: ((Error) -> Void)?
    private var samples = [Float]()
    private var rate = 48000.0
    private var endTime = 0.0
    private var counter = 0
    private var newFrames = 0
    private var failed = false
    private let recording: MeetingRecording?
    init(directory: URL, recording: MeetingRecording? = nil) {
        self.directory = directory; self.recording = recording
    }
    func add(_ values: [Float], rate: Double, timestamp: Double) {
        queue.async {
            guard !self.failed, rate > 0 else { return }
            do { try self.recording?.append(values, rate: rate, timestamp: timestamp) }
            catch {
                self.failed = true
                DispatchQueue.main.async { self.onError?(error) }
                return
            }
            if self.rate != rate {
                self.flushTail()
                self.samples.removeAll(); self.newFrames = 0; self.rate = rate
            }
            self.samples.append(contentsOf: values); self.newFrames += values.count
            self.endTime = timestamp
            let count = Int(rate * 6), overlap = Int(rate * 0.7)
            while self.samples.count >= count {
                let time = timestamp - Double(self.samples.count - count) / rate
                self.write(Array(self.samples.prefix(count)), timestamp: time)
                self.samples.removeFirst(count - overlap)
                self.newFrames = max(0, self.samples.count - overlap)
            }
        }
    }
    func finish(_ completion: @escaping () -> Void) {
        queue.async {
            self.flushTail(); self.samples.removeAll(); self.newFrames = 0
            do { try self.recording?.finish() }
            catch { DispatchQueue.main.async { self.onError?(error) } }
            DispatchQueue.main.async(execute: completion)
        }
    }
    private func flushTail() {
        if newFrames > 0 && samples.count > Int(rate * 0.05) { write(samples, timestamp: endTime) }
    }
    private func write(_ values: [Float], timestamp: Double) {
        guard !failed, !values.isEmpty else { return }
        let rms = sqrt(values.reduce(0.0) { $0 + Double($1) * Double($1) } / Double(values.count))
        guard rms >= 0.0003 else { return }
        counter += 1
        let path = directory.appendingPathComponent(String(format: "chunk-%08d.wav", counter))
        do {
            let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(values.count))!
            buffer.frameLength = AVAudioFrameCount(values.count)
            values.withUnsafeBufferPointer { buffer.floatChannelData![0].update(from: $0.baseAddress!, count: $0.count) }
            try autoreleasepool { let file = try AVAudioFile(forWriting: path, settings: format.settings); try file.write(from: buffer) }
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
            let chunk = AudioChunk(id: counter, path: path, timestamp: timestamp, duration: Double(values.count) / rate)
            var record = chunk.request; record["duration"] = chunk.duration
            try JSONSerialization.data(withJSONObject: record).write(to: chunk.metadata, options: .atomic)
            DispatchQueue.main.async { self.onChunk?(chunk) }
        } catch {
            failed = true
            DispatchQueue.main.async { self.onError?(error) }
        }
    }
}

final class Diagnostics {
    private let queue = DispatchQueue(label: "local.meeting.diagnostics")
    private let url: URL
    init(directory: URL) { url = directory.appendingPathComponent("diagnostics.jsonl") }
    func record(_ fields: [String: Any]) {
        queue.async {
            var record = fields; record["time"] = Date().timeIntervalSince1970
            guard var data = try? JSONSerialization.data(withJSONObject: record, options: .sortedKeys) else { return }
            data.append(10)
            let fm = FileManager.default
            if let size = (try? fm.attributesOfItem(atPath: self.url.path)[.size]) as? Int, size > 2_000_000 {
                let previous = self.url.appendingPathExtension("previous")
                try? fm.removeItem(at: previous); try? fm.moveItem(at: self.url, to: previous)
            }
            if !fm.fileExists(atPath: self.url.path) { fm.createFile(atPath: self.url.path, contents: nil, attributes: [.posixPermissions: 0o600]) }
            if let handle = try? FileHandle(forWritingTo: self.url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd(); try? handle.write(contentsOf: data)
            }
            try? data.write(to: self.url.deletingLastPathComponent().appendingPathComponent("last-status.json"), options: .atomic)
        }
    }
}
