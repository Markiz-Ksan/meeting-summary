import Foundation

@main
struct AudioMixerChecks {
    final class Clock {
        var now: Double
        init(_ now: Double) { self.now = now }
    }
    final class Output {
        private let lock = NSLock()
        private var savedValues = [Float]()
        private var savedBatches = [(Int, Double)]()
        var values: [Float] { lock.lock(); defer { lock.unlock() }; return savedValues }
        var batches: [(Int, Double)] { lock.lock(); defer { lock.unlock() }; return savedBatches }
        func attach(_ mixer: AudioMixer) {
            mixer.onSamples = { values, rate, timestamp in
                precondition(rate == 48000)
                self.lock.lock()
                self.savedValues.append(contentsOf: values)
                self.savedBatches.append((values.count, timestamp))
                self.lock.unlock()
            }
        }
    }
    static func expect(_ condition: @autoclosure () -> Bool, _ text: String) {
        guard condition() else { fatalError(text) }
    }
    static func finish(_ mixer: AudioMixer) {
        var done = false
        mixer.flush { expect(Thread.isMainThread, "flush completion must be main"); done = true }
        let deadline = Date().addingTimeInterval(3)
        while !done && Date() < deadline {
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.01))
        }
        expect(done, "flush timed out")
    }
    static func main() {
        for source in [AudioSource.system, .microphone] {
            let mixer = AudioMixer(enabledSources: [.system, .microphone], clock: { 1000.25 }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            mixer.add([Float](repeating: 0.3, count: 12000), rate: 48000, source: source, timestamp: 1000.25)
            mixer.tick(at: 1000.45)
            // A queue barrier proves the tick itself delivered, not just flush.
            _ = mixer.statistics
            expect(output.values.count == 12000, "missing source stalled its peer")
            finish(mixer)
            expect(output.values.count == 12000, "flush duplicated the complete batch")
            expect(output.values.allSatisfy { abs($0 - 0.3) < 0.00001 }, "single source changed amplitude")
        }
        print("PASS: either source works without its peer; flush does not duplicate")

        do {
            let mixer = AudioMixer(enabledSources: [.system, .microphone], clock: { 1000.5 }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            mixer.add([Float](repeating: 0.2, count: 12000), rate: 48000, source: .system, timestamp: 1000.25)
            mixer.add([Float](repeating: 0.3, count: 6000), rate: 24000, source: .microphone, timestamp: 1000.25)
            mixer.add([Float](repeating: 0.4, count: 11025), rate: 44100, source: .system, timestamp: 1000.5)
            mixer.add([Float](repeating: 0.1, count: 12000), rate: 48000, source: .microphone, timestamp: 1000.5)
            finish(mixer)
            expect(output.values.count == 24000, "aligned streams were concatenated instead of mixed")
            expect(output.values.allSatisfy { abs($0 - 0.5) < 0.00001 }, "aligned streams did not sum on the same timeline")
            expect(output.batches.last!.1 == 1000.5, "mixed end timestamp drifted")
        }
        print("PASS: aligned 24/44.1/48 kHz streams sum, keeping duration and timestamps")

        for rate in [24000.0, 44100.0, 48000.0] {
            let mixer = AudioMixer(enabledSources: [.system], clock: { 1000.5 }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            for part in 0..<2 {
                let count = Int(rate * 0.25)
                let samples = (0..<count).map { Float(sin(2 * .pi * 100 * (Double(part) * 0.25 + Double($0) / rate))) * 0.4 }
                mixer.add(samples, rate: rate, source: .system, timestamp: 1000 + Double(part + 1) * 0.25)
            }
            finish(mixer)
            expect(output.values.count == 24000, "resampler changed the interval length")
            let largestError = output.values.enumerated().map { abs(Double($0.element) - sin(2 * .pi * 100 * Double($0.offset) / 48000) * 0.4) }.max()!
            expect(largestError < 0.0002, "resampling phase discontinuity at \(rate): \(largestError)")
        }
        print("PASS: linear resampling preserves sine phase across packet boundaries")

        do {
            let mixer = AudioMixer(enabledSources: [.system], clock: { 1000.75 }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            for (index, jitter) in [0.0, 0.008, -0.006].enumerated() {
                mixer.add([Float](repeating: 0.2, count: 12000), rate: 48000,
                          source: .system, timestamp: 1000 + Double(index + 1) * 0.25 + jitter)
            }
            mixer.tick(at: 1000.95)
            // A duplicate short tail must not be mistaken for a jittered new packet.
            mixer.add([Float](repeating: 0.9, count: 960), rate: 48000, source: .system, timestamp: 1000.75)
            finish(mixer)
            expect(output.values.count == 36000, "jitter or duplicate short tail changed duration")
            expect(output.values.allSatisfy { abs($0 - 0.2) < 0.00001 }, "jitter inserted gaps or duplicate samples")
        }
        print("PASS: callback jitter stays continuous; replayed short tails are discarded")

        do {
            let mixer = AudioMixer(enabledSources: [.system, .microphone], clock: { 1000.5 }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            mixer.add([Float](repeating: 0.2, count: 12000), rate: 48000, source: .system, timestamp: 1000.25)
            mixer.add([Float](repeating: 0.6, count: 12000), rate: 48000, source: .microphone, timestamp: 1000.25)
            mixer.setEnabled([.system])
            mixer.tick(at: 1000.45)
            mixer.add([Float](repeating: 0.9, count: 12000), rate: 48000, source: .microphone, timestamp: 1000.25)
            // An already-played packet cannot rewrite emitted audio or delay it.
            mixer.add([Float](repeating: 0.9, count: 12000), rate: 48000, source: .system, timestamp: 1000.25)
            let stats = mixer.statistics
            expect(stats.lateFrames == 12000, "late packet was not discarded")
            finish(mixer)
            expect(output.values.count == 12000, "mute/late packet changed duration")
            expect(output.values.allSatisfy { abs($0 - 0.2) < 0.00001 }, "mute retained buffered microphone audio")
        }
        print("PASS: mute removes buffered microphone; late packets cannot duplicate output")

        do {
            let clock = Clock(1000.3)
            let mixer = AudioMixer(enabledSources: [.system], clock: { clock.now }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            mixer.add([Float](repeating: 0.25, count: 14400), rate: 48000, source: .system, timestamp: 1000.3)
            finish(mixer)
            expect(output.values.count == 14400 && output.batches.count == 2, "partial final tail was lost")
            finish(mixer)
            expect(output.values.count == 14400, "second flush repeated the tail")
            expect(zip(output.batches, output.batches.dropFirst()).allSatisfy { $0.0.1 < $0.1.1 }, "timestamps are not monotonic")
        }
        print("PASS: partial tail flushed exactly once with monotonic timestamps")

        do {
            let mixer = AudioMixer(enabledSources: [.system], clock: { 1000.25 }, automaticTick: false)
            let output = Output(); output.attach(mixer)
            mixer.add([Float](repeating: 0.1, count: 12000), rate: 48000, source: .system, timestamp: 1000.25)
            mixer.tick(at: 4600.25)
            let stats = mixer.statistics
            expect(stats.discardedGapFrames > 0, "sleep discontinuity was not bounded")
            expect(output.values.count <= 96000, "wake emitted an hour of silence")
            expect(stats.bufferedFrames <= 108000, "retained buffer exceeded its two-second bound")
            finish(mixer)
        }
        print("PASS: long gaps and retained buffers are bounded")

        do {
            let mixer = AudioMixer(enabledSources: [.system, .microphone])
            let output = Output(); output.attach(mixer)
            let end = Date().timeIntervalSince1970
            mixer.add([Float](repeating: 0.1, count: 12000), rate: 48000, source: .microphone, timestamp: end)
            let deadline = Date().addingTimeInterval(1)
            while Date() < deadline {
                _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.02))
                _ = mixer.statistics
                if !output.values.isEmpty { break }
            }
            _ = mixer.statistics
            expect(!output.values.isEmpty, "real timer did not emit a microphone-only stream")
            finish(mixer)
        }
        print("PASS: production timer delivers even when system audio never arrives")
    }
}
