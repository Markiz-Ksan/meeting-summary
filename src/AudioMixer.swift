import Foundation

enum AudioSource: Hashable {
    case system
    case microphone
}

/// Aligns independent mono sources on one wall-clock timeline. Missing sources
/// contribute silence; neither source can hold up delivery of the other.
final class AudioMixer {
    private final class SourceBuffer {
        var blocks = [Int64: [Float]]()
        var endFrame: Double?
    }

    private let queue = DispatchQueue(label: "local.meeting.audio-mixer", qos: .userInitiated)
    private let queueKey = DispatchSpecificKey<Bool>()
    private let outputRate: Double
    private let blockFrames: Int
    private let maximumFrames: Int64
    private let clock: () -> Double
    private let automaticTick: Bool
    private let holdback = 0.2
    private let jitterTolerance = 0.03
    private var enabledSources: Set<AudioSource>
    private var buffers = [AudioSource: SourceBuffer]()
    private var timer: DispatchSourceTimer?
    private var origin: Double?
    private var cursor: Int64?
    private var emitted = false
    private var latestEndFrame: Int64?
    private var finished = false
    private var handler: (([Float], Double, Double) -> Void)?
    private var lateFrames: Int64 = 0
    private var discardedGapFrames: Int64 = 0

    /// Called on the mixer's serial queue. The timestamp is the end of this
    /// output batch, never the arrival time of either input callback.
    var onSamples: (([Float], Double, Double) -> Void)? {
        get { synchronized { handler } }
        set { synchronized { handler = newValue } }
    }

    // Optional clock/timer seams keep tests deterministic without audio devices.
    init(enabledSources: Set<AudioSource>, outputRate: Double = 48000,
         clock: @escaping () -> Double = { Date().timeIntervalSince1970 },
         automaticTick: Bool = true) {
        precondition(outputRate.isFinite && outputRate >= 8000 && outputRate <= 192000)
        self.enabledSources = enabledSources
        self.outputRate = outputRate
        self.blockFrames = max(1, Int((outputRate * 0.25).rounded()))
        self.maximumFrames = Int64(outputRate * 2)
        self.clock = clock
        self.automaticTick = automaticTick
        queue.setSpecific(key: queueKey, value: true)
    }

    deinit { timer?.cancel() }

    private func synchronized<T>(_ body: () -> T) -> T {
        if DispatchQueue.getSpecific(key: queueKey) == true { return body() }
        return queue.sync(execute: body)
    }

    func setEnabled(_ sources: Set<AudioSource>) {
        queue.async {
            guard !self.finished else { return }
            self.enabledSources = sources
            self.buffers = self.buffers.filter { sources.contains($0.key) }
            // Buffered microphone samples disappear immediately on mute.
            if sources.isEmpty {
                self.buffers.removeAll()
                self.latestEndFrame = self.cursor
            }
        }
    }

    func add(_ values: [Float], rate: Double, source: AudioSource, timestamp: Double) {
        queue.async {
            guard !self.finished, self.enabledSources.contains(source),
                  !values.isEmpty, rate.isFinite, rate >= 8000, rate <= 192000,
                  timestamp.isFinite else { return }
            let now = self.clock()
            guard now.isFinite, timestamp <= now + 2, timestamp >= now - 10 else { return }
            // An unexpectedly large producer packet cannot allocate an unbounded
            // resampling buffer. Keep its newest two seconds with the same end.
            let input = values.suffix(min(values.count, Int(rate * 2)))
            let duration = Double(input.count) / rate
            if self.origin == nil {
                self.origin = timestamp - duration
                self.cursor = 0
                self.startTimer()
            }
            guard let origin = self.origin, let cursor = self.cursor else { return }
            let observedStart = (timestamp - origin) * self.outputRate - duration * self.outputRate
            let observedEnd = observedStart + duration * self.outputRate
            let state = self.buffers[source] ?? SourceBuffer()
            // Callback scheduling jitter must not insert tiny gaps every 250 ms.
            // Larger discontinuities retain their true position on the timeline.
            let start: Double
            if let previousEnd = state.endFrame, observedEnd > previousEnd + 0.5,
               abs(observedStart - previousEnd) <= self.jitterTolerance * self.outputRate {
                start = previousEnd
            } else { start = abs(observedStart) < 0.02 ? 0 : observedStart }
            let end = start + duration * self.outputRate
            let first = Int64(ceil(start - 0.00001))
            let last = Int64(ceil(end - 0.00001))
            if self.emitted, last <= cursor {
                self.lateFrames += max(0, last - first)
                return
            }
            if !self.emitted { self.cursor = min(cursor, first) }
            let writableStart = max(first, self.cursor!)
            let futureLimit = Int64(((now - origin) * self.outputRate).rounded()) + self.maximumFrames
            let writableEnd = min(last, futureLimit)
            guard writableEnd > writableStart else { return }
            if first < writableStart { self.lateFrames += writableStart - first }
            self.buffers[source] = state
            state.endFrame = max(state.endFrame ?? end, end)
            self.latestEndFrame = max(self.latestEndFrame ?? writableEnd, writableEnd)

            input.withUnsafeBufferPointer { samples in
                var frame = writableStart
                while frame < writableEnd {
                    let key = self.blockKey(frame)
                    let blockStart = key * Int64(self.blockFrames)
                    let stop = min(writableEnd, blockStart + Int64(self.blockFrames))
                    var block = state.blocks.removeValue(forKey: key) ?? [Float](repeating: 0, count: self.blockFrames)
                    while frame < stop {
                        let position = max(0, (Double(frame) - start) * rate / self.outputRate)
                        let left = min(samples.count - 1, Int(position))
                        let value: Float
                        if left + 1 < samples.count {
                            let fraction = Float(position - Double(left))
                            value = samples[left] + fraction * (samples[left + 1] - samples[left])
                        } else if samples.count > 1 {
                            // Linear extrapolation for the final upsampled point
                            // avoids a held sample/phase kink at each packet edge.
                            let fraction = Float(min(1, position - Double(left)))
                            value = samples[left] + fraction * (samples[left] - samples[left - 1])
                        } else { value = samples[left] }
                        block[Int(frame - blockStart)] = value.isFinite ? value : 0
                        frame += 1
                    }
                    state.blocks[key] = block
                }
            }
            self.boundBuffer(now: now)
        }
    }

    private func startTimer() {
        guard automaticTick, timer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 0.05, repeating: 0.05, leeway: .milliseconds(5))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.tickLocked(at: self.clock())
        }
        self.timer = timer
        timer.resume()
    }

    /// Deterministic tick seam; production delivery is driven by the timer.
    func tick(at timestamp: Double) { queue.async { self.tickLocked(at: timestamp) } }

    private func tickLocked(at timestamp: Double) {
        guard !finished, !enabledSources.isEmpty, timestamp.isFinite, let origin else { return }
        let ready = Int64(floor((timestamp - holdback - origin) * outputRate + 0.00001))
        emit(until: ready, includePartial: false)
    }

    private func blockKey(_ frame: Int64) -> Int64 {
        Int64(floor(Double(frame) / Double(blockFrames)))
    }

    private func boundBuffer(now: Double) {
        guard let origin, let cursor else { return }
        let oldest = max(cursor, Int64(((now - origin) * outputRate).rounded()) - maximumFrames)
        for state in buffers.values {
            state.blocks = state.blocks.filter { ($0.key + 1) * Int64(blockFrames) > oldest }
        }
    }

    private func emit(until requestedEnd: Int64, includePartial: Bool) {
        guard let origin, var next = cursor, requestedEnd > next else { return }
        if requestedEnd - next > maximumFrames {
            // After sleep or a stalled producer, resume near live time instead
            // of emitting minutes of synthetic silence on a single timer tick.
            let resumed = requestedEnd - maximumFrames
            discardedGapFrames += resumed - next
            next = resumed
        }
        while next < requestedEnd {
            let count = Int(min(Int64(blockFrames), requestedEnd - next))
            if count < blockFrames && !includePartial { break }
            var mixed = [Float](repeating: 0, count: count)
            var frame = next
            while frame < next + Int64(count) {
                let key = blockKey(frame)
                let blockStart = key * Int64(blockFrames)
                let stop = min(next + Int64(count), blockStart + Int64(blockFrames))
                for source in enabledSources {
                    guard let block = buffers[source]?.blocks[key] else { continue }
                    for position in frame..<stop {
                        mixed[Int(position - next)] += block[Int(position - blockStart)]
                    }
                }
                frame = stop
            }
            for index in mixed.indices { mixed[index] = min(1, max(-1, mixed[index])) }
            next += Int64(count)
            cursor = next
            emitted = true
            handler?(mixed, outputRate, origin + Double(next) / outputRate)
        }
        cursor = next
        for state in buffers.values {
            state.blocks = state.blocks.filter { ($0.key + 1) * Int64(blockFrames) > next }
        }
    }

    /// Flushes the newest actual input once, stops the timer, and rejects late
    /// arrivals. All sample callbacks precede completion on the main queue.
    func flush(completion: @escaping () -> Void) {
        queue.async {
            if !self.finished {
                self.timer?.cancel(); self.timer = nil
                if let end = self.latestEndFrame { self.emit(until: end, includePartial: true) }
                self.finished = true
                self.buffers.removeAll()
            }
            DispatchQueue.main.async(execute: completion)
        }
    }

    var statistics: (bufferedFrames: Int, lateFrames: Int64, discardedGapFrames: Int64) {
        synchronized {
            (buffers.values.reduce(0) { $0 + $1.blocks.count * blockFrames }, lateFrames, discardedGapFrames)
        }
    }
}
