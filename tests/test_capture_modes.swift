import Foundation
import AVFoundation

/// Synthetic inputs drive the production controller and mixer. No hardware or
/// microphone permissions are touched by these checks.
@main
struct CaptureModeChecks {
    final class TestClock {
        private let lock = NSLock()
        private var value: Double
        init(_ now: Double = 1000.25) { value = now }
        var now: Double {
            get { lock.lock(); defer { lock.unlock() }; return value }
            set { lock.lock(); value = newValue; lock.unlock() }
        }
    }
    final class Collected {
        private let lock = NSLock()
        private var storage = [Float]()
        private var rates = [Double]()
        func append(_ values: [Float], rate: Double) {
            lock.lock(); storage.append(contentsOf: values); rates.append(rate); lock.unlock()
        }
        var values: [Float] { lock.lock(); defer { lock.unlock() }; return storage }
        var allRatesCorrect: Bool { lock.lock(); defer { lock.unlock() }; return rates.allSatisfy { $0 == 48000 } }
    }
    final class FakeInput: CaptureInput {
        var onSamples: (([Float], Double) -> Void)?
        var onError: ((Error) -> Void)?
        let captureUID: String
        let failsToStart: Bool
        var deferred = false
        var pendingStart: ((Result<Void, Error>) -> Void)?
        func completeStart() { pendingStart?(.success(())); pendingStart = nil }
        var starts = 0
        var stops = 0
        var tail = [Float]()
        var tailRate: Double = 48000
        var startupError: NSError = FakeInput.failure
        var delayStop = false
        private var pendingStops = [() -> Void]()
        init(uid: String, failsToStart: Bool = false) {
            captureUID = uid; self.failsToStart = failsToStart
        }
        func start(completion: @escaping (Result<Void, Error>) -> Void) {
            starts += 1
            if deferred { pendingStart = completion; return }
            completion(failsToStart ? .failure(startupError) : .success(()))
        }
        func stop(completion: (() -> Void)?) {
            stops += 1
            if delayStop {
                pendingStops.append { completion?() }
                return
            }
            emitTail()
            DispatchQueue.main.async { completion?() }
        }
        private func emitTail() {
            if !tail.isEmpty { onSamples?(tail, tailRate); tail.removeAll() }
        }
        func completeDelayedStop() {
            delayStop = false
            emitTail()
            let callbacks = pendingStops; pendingStops.removeAll()
            for callback in callbacks { DispatchQueue.main.async(execute: callback) }
        }
        func emit(_ value: Float, frames: Int = 12000, rate: Double = 48000) {
            onSamples?([Float](repeating: value, count: frames), rate)
        }
        func fail() { onError?(Self.failure) }
        static var failure: NSError { NSError(domain: "SyntheticCapture", code: 7, userInfo: [NSLocalizedDescriptionKey: "Synthetic device unavailable"]) }
    }
    final class InputStore {
        var created = [AudioSource: [FakeInput]]()
        var requestedUIDs = [AudioSource: [String?]]()
        var failing = Set<AudioSource>()
        var deferred = Set<AudioSource>()
        var failureErrors = [AudioSource: NSError]()
        var currentUIDs = [AudioSource: String]()
        func make(_ source: AudioSource, uid: String?) -> CaptureInput {
            let input = FakeInput(uid: uid ?? currentUIDs[source] ?? Self.uid(source), failsToStart: failing.contains(source))
            input.deferred = deferred.contains(source)
            input.startupError = failureErrors[source] ?? FakeInput.failure
            created[source, default: []].append(input)
            requestedUIDs[source, default: []].append(uid)
            return input
        }
        func latest(_ source: AudioSource) -> FakeInput { created[source]!.last! }
        static func uid(_ source: AudioSource) -> String { source == .system ? "synthetic-output" : "synthetic-input" }
    }
    final class Permission { var value: AVAuthorizationStatus = .authorized }
    final class Harness {
        let clock = TestClock()
        let store = InputStore()
        let output = Collected()
        let mixer: AudioMixer
        let controller: CaptureController
        var results = [Bool]()
        let permission = Permission()
        init(mode: CaptureMode, failures: Set<AudioSource> = [], selectedMicrophone: String? = nil, failureErrors: [AudioSource: NSError] = [:], deferred: Set<AudioSource> = [], authorization: AVAuthorizationStatus = .authorized) {
            let clock = self.clock, store = self.store, output = self.output, permission = self.permission
            self.permission.value = authorization
            store.deferred = deferred
            store.failing = failures; store.failureErrors = failureErrors
            mixer = AudioMixer(enabledSources: mode.sources, clock: { clock.now }, automaticTick: false)
            controller = CaptureController(mode: mode, microphoneUID: selectedMicrophone, mixer: mixer,
                factory: { store.make($0, uid: $1) }, currentUID: { store.currentUIDs[$0] ?? InputStore.uid($0) },
                clock: { Date(timeIntervalSince1970: clock.now) }, authorization: { permission.value })
            controller.onSamples = { values, rate, _ in output.append(values, rate: rate) }
            controller.start { [weak self] result in
                switch result { case .success: self?.results.append(true); case .failure: self?.results.append(false) }
            }
        }
        func drain() {
            var mainDrained = false
            DispatchQueue.main.async { mainDrained = true }
            CaptureModeChecks.wait(until: { mainDrained }, "Main callback queue did not drain")
            _ = mixer.statistics
        }
        func tickMixer() {
            drain(); mixer.tick(at: clock.now + 0.2); _ = mixer.statistics
        }
        func finish() {
            var stopped = false
            controller.stop { stopped = true }
            CaptureModeChecks.wait(until: { stopped }, "Controller did not complete its final audio drain")
            expect(!controller.isCapturing, "Controller remained active after stop")
            expect(output.allRatesCorrect, "Mixer output changed its sample rate")
        }
    }
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else { fatalError(message) }
    }
    static func wait(until condition: () -> Bool, _ message: String) {
        let deadline = Date().addingTimeInterval(3)
        while !condition() && Date() < deadline {
            _ = RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        expect(condition(), message)
    }
    static func equalSamples(_ values: [Float], _ expected: Float, _ message: String) {
        expect(!values.isEmpty && values.allSatisfy { abs($0 - expected) < 0.00001 }, message)
    }
    static func main() {
        for gap in [false, true] {
            let h = Harness(mode: .microphone, deferred: [.microphone], authorization: .notDetermined)
            let mic = h.store.latest(.microphone)
            if !gap { h.clock.now += 90; h.controller.tick(); h.drain() }
            h.clock.now += 120; h.permission.value = .authorized
            h.controller.tick(); h.drain()
            expect(mic.stops == 0 && h.results.isEmpty, "Grant after long permission wait incorrectly timed out")
            mic.completeStart(); h.drain(); mic.emit(0.3); h.tickMixer()
            expect(h.results == [true] && !h.output.values.isEmpty, "Post-grant startup did not deliver audio")
            h.finish()
        }
        print("PASS: delayed grants including long polling gaps get a fresh hardware startup budget")
        do {
            let h = Harness(mode: .microphone, deferred: [.microphone], authorization: .notDetermined)
            h.clock.now += 90; h.permission.value = .authorized; h.controller.tick()
            h.clock.now += 29; h.controller.tick(); h.drain()
            expect(h.results.isEmpty, "Post-grant hardware budget ended early")
            h.clock.now += 2; h.controller.tick(); h.drain()
            expect(h.results == [false], "Real post-grant hardware hang did not time out")
            h.finish()
        }
        do {
            let h = Harness(mode: .both, deferred: [.system,.microphone], authorization: .notDetermined)
            h.clock.now += 31; h.controller.tick(); h.drain()
            expect(h.store.latest(.system).stops == 1 && h.store.latest(.microphone).stops == 0, "Permission wait changed independent system deadline")
            h.finish(); h.permission.value = .authorized; h.store.latest(.microphone).completeStart(); h.drain()
            expect(h.results.isEmpty && h.output.values.isEmpty, "Late permission callback escaped cancellation")
        }

        do {
            let h = Harness(mode: .microphone, selectedMicrophone: "chosen-usb-microphone")
            expect(h.store.created[.system] == nil, "Microphone-only mode constructed system capture")
            expect(h.store.latest(.microphone).starts == 1, "Microphone-only mode did not start its selected input")
            expect(h.store.latest(.microphone).captureUID == "chosen-usb-microphone", "Selected microphone UID was lost")
            h.store.latest(.microphone).emit(0.3)
            h.drain(); h.finish()
            expect(h.output.values.count == 12000, "Microphone-only mode lost or duplicated audio")
            equalSamples(h.output.values, 0.3, "Microphone-only mode altered its audio")
            expect(h.results == [true], "Microphone-only startup completion was not reported once")
        }
        print("PASS: microphone-only mode never creates system capture and uses the selected microphone")

        for unavailable in [AudioSource.system, .microphone] {
            let healthy: AudioSource = unavailable == .system ? .microphone : .system
            let h = Harness(mode: .both, failures: [unavailable])
            let peer = h.store.latest(healthy)
            expect(peer.stops == 0 && peer.starts == 1, "One startup failure stopped the healthy input")
            expect(h.controller.isCapturing && h.results == [true], "Partial startup success was treated as total failure")
            peer.emit(0.25); h.tickMixer()
            expect(h.output.values.count == 12000, "Failed input stalled delivery from the healthy peer")
            equalSamples(h.output.values, 0.25, "Healthy peer audio changed after other input failed")
            expect(peer.stops == 0, "Healthy peer was stopped before requested shutdown")
            h.finish()
        }
        print("PASS: either input can fail at startup while its peer continues delivering audio")

        for unavailable in [AudioSource.system, .microphone] {
            let healthy: AudioSource = unavailable == .system ? .microphone : .system
            let h = Harness(mode: .both)
            let bad = h.store.latest(unavailable), peer = h.store.latest(healthy)
            let staleCallback = bad.onSamples
            bad.fail(); h.drain()
            staleCallback?([Float](repeating: 0.9, count: 12000),48000)
            peer.emit(0.2); h.tickMixer()
            expect(peer.stops == 0 && h.controller.isCapturing, "Runtime failure stopped healthy capture")
            equalSamples(h.output.values,0.2,"Failed input leaked stale callbacks into the mix")
            h.finish()
        }
        print("PASS: runtime failures are isolated and late failed-input callbacks are ignored")

        do {
            let h = Harness(mode: .both)
            let mic = h.store.latest(.microphone), system = h.store.latest(.system)
            let staleMicCallback = mic.onSamples
            mic.emit(0.7); system.emit(0.2); h.drain()
            expect(h.mixer.statistics.bufferedFrames > 0, "Mute test did not queue audio")
            h.controller.setMicrophoneMuted(true)
            staleMicCallback?([Float](repeating: 0.8,count:12000),48000)
            h.drain(); h.finish()
            expect(mic.stops == 1, "Mute did not stop microphone capture exactly once")
            expect(system.stops == 1, "Mute unexpectedly stopped system capture before final stop")
            expect(h.output.values.count == 12000, "Muting changed the timeline of queued system audio")
            equalSamples(h.output.values,0.2,"Queued microphone audio survived mute")
        }
        print("PASS: microphone mute removes queued and late microphone samples while preserving system audio")

        do {
            let h = Harness(mode: .both)
            let system = h.store.latest(.system), oldMic = h.store.latest(.microphone)
            system.emit(0.1); oldMic.emit(0.2); h.tickMixer()
            h.controller.setMicrophoneMuted(true); h.drain()
            h.clock.now = 1000.5; system.emit(0.15); oldMic.emit(0.8); h.tickMixer()
            h.controller.setMicrophoneMuted(false); h.drain()
            let newMic = h.store.latest(.microphone)
            expect(newMic !== oldMic, "Unmute reused a stopped microphone")
            expect(h.store.created[.system]?.count == 1 && system.stops == 0, "Unmute restarted system capture")
            h.clock.now = 1000.75; system.emit(0.15); newMic.emit(0.2); h.tickMixer(); h.finish()
            let values = h.output.values
            expect(values.count == 36000, "Mute/unmute changed mixed audio duration")
            equalSamples(Array(values[0..<12000]),0.3,"Initial mixed audio was incorrect")
            equalSamples(Array(values[12000..<24000]),0.15,"Muted microphone was still included")
            equalSamples(Array(values[24000..<36000]),0.35,"Unmuted microphone did not rejoin the timeline")
        }
        print("PASS: microphone unmute rejoins the timeline without restarting system audio")

        do {
            let h = Harness(mode: .both)
            let system = h.store.latest(.system), mic = h.store.latest(.microphone)
            let systemLate = system.onSamples, micLate = mic.onSamples
            system.tail = [Float](repeating:0.2,count:720)
            mic.tail = [Float](repeating:0.3,count:720)
            h.finish()
            expect(h.output.values.count == 720, "Stop did not drain both partial tails exactly once")
            equalSamples(h.output.values,0.5,"Stop failed to mix both sources' final tails")
            let before = h.output.values.count
            systemLate?([Float](repeating:0.8,count:12000),48000)
            micLate?([Float](repeating:0.8,count:12000),48000)
            h.clock.now = 1001; h.drain(); h.controller.tick(); h.mixer.tick(at:1001.2); _ = h.mixer.statistics
            expect(h.output.values.count == before,"Audio callback escaped after stop completion")
            expect(h.store.created.values.reduce(0) { $0+$1.count } == 2,"Stopped controller restarted an input")
        }
        print("PASS: stop drains both partial tails and blocks late callbacks and reconnection")

        for stalled in [AudioSource.system, .microphone] {
            let healthy: AudioSource = stalled == .system ? .microphone : .system
            let h = Harness(mode:.both)
            let old = h.store.latest(stalled), peer = h.store.latest(healthy)
            old.emit(0.3); peer.emit(0.2); h.tickMixer()
            h.clock.now = 1009.5; peer.emit(0.2); h.drain(); h.controller.tick(); h.drain(); h.tickMixer()
            expect(h.store.created[stalled]?.count == 2, "Stalled source did not reconnect independently")
            expect(old.stops == 1, "Stalled source was not stopped before replacement")
            expect(h.store.created[healthy]?.count == 1 && peer.stops == 0, "Stalled source restarted the healthy peer")
            equalSamples(Array(h.output.values.suffix(12000)),0.2,"Healthy source stopped delivering during peer reconnection")
            h.finish()
        }
        print("PASS: a stalled source reconnects without blocking or restarting its healthy peer")
        for mode in [CaptureMode.microphone, .both] {
            let h = Harness(mode:mode,failures:mode.sources)
            expect(h.results.isEmpty, "A transient initial failure completed startup before retries")
            h.store.failing.remove(.microphone)
            h.clock.now += 2.25; h.controller.tick(); h.drain()
            expect(h.results == [true] && h.controller.isCapturing, "A successful retry did not complete startup exactly once")
            expect(h.store.created[.microphone]?.count == 2, "Transient microphone failure was not retried")
            h.store.latest(.microphone).emit(0.3); h.tickMixer()
            equalSamples(h.output.values,0.3,"Recovered input did not deliver audio: mode=\(mode.rawValue), samples=\(h.output.values.count), min=\(h.output.values.min() ?? -2), max=\(h.output.values.max() ?? -2)")
            h.finish()
        }
        print("PASS: initial transient failures remain pending until a retry succeeds")

        do {
            let h = Harness(mode:.both,failures:[.system,.microphone])
            expect(h.results.isEmpty,"Initial failure was reported before retry budget was exhausted")
            for retry in 1...5 {
                h.clock.now += 12; h.controller.tick(); h.drain()
                if retry < 5 { expect(h.results.isEmpty,"Startup failed while retry attempts remained") }
            }
            expect(h.results == [false],"Exhausting every source did not report one terminal startup failure")
            expect(!h.controller.isCapturing,"Exhausted capture was still marked active")
            expect(h.store.created[.system]?.count == 6 && h.store.created[.microphone]?.count == 6,"Retry budget was not bounded per input")
            h.clock.now += 120; h.controller.tick(); h.drain()
            expect(h.store.created.values.reduce(0) { $0+$1.count } == 12,"Exhausted sources retried indefinitely")
            h.finish()
        }
        print("PASS: total startup failure is reported only after all bounded retries are exhausted")

        do {
            let denied = NSError(domain:"MeetingMicrophone",code:2,userInfo:[NSLocalizedDescriptionKey:"Synthetic permission denial"])
            let h = Harness(mode:.microphone,failures:[.microphone],failureErrors:[.microphone:denied])
            expect(h.results == [false],"Permanent microphone permission denial did not fail immediately")
            h.clock.now += 120; h.controller.tick(); h.drain()
            expect(h.store.created[.microphone]?.count == 1,"Permission denial caused repeated microphone requests")
            h.finish()
            let mixed = Harness(mode:.both,failures:[.system,.microphone],failureErrors:[.microphone:denied])
            expect(mixed.results.isEmpty,"Denied microphone incorrectly ended the retryable system source")
            mixed.store.failing.remove(.system)
            mixed.clock.now += 2.25; mixed.controller.tick(); mixed.drain()
            expect(mixed.results == [true],"System retry failed to recover after independent mic permission denial")
            expect(mixed.store.created[.microphone]?.count == 1,"Denied mic was restarted with the system source")
            mixed.finish()
        }
        print("PASS: microphone permission denial is permanent without preventing system-source recovery")

        do {
            let h = Harness(mode:.both)
            let oldSystem = h.store.latest(.system), oldMic = h.store.latest(.microphone)
            oldSystem.tail = [Float](repeating:0.2,count:720)
            oldMic.tail = [Float](repeating:0.3,count:720)
            h.controller.wake(); h.drain()
            expect(h.store.created[.system]?.count == 2 && h.store.created[.microphone]?.count == 2,"Wake did not reconnect both sources")
            expect(oldSystem.stops == 1 && oldMic.stops == 1,"Wake did not drain each old source once")
            h.finish()
            expect(h.output.values.count == 720,"Wake discarded or duplicated pending final samples")
            equalSamples(h.output.values,0.5,"Wake invalidated old sources before their tails reached the mixer")
        }
        print("PASS: wake drains both old-source tails before replacing their generations")

        for source in [AudioSource.system,.microphone] {
            let peer: AudioSource = source == .system ? .microphone : .system
            let h = Harness(mode:.both)
            let old = h.store.latest(source), healthy = h.store.latest(peer)
            old.tail = [Float](repeating:0.3,count:720)
            healthy.emit(0.2)
            h.store.currentUIDs[source] = "replacement-device"
            h.controller.tick(); h.drain()
            expect(h.store.created[source]?.count == 2,"Default device change did not reconnect its source")
            expect(h.store.created[peer]?.count == 1 && healthy.stops == 0,"Default device reconnect interrupted its peer")
            h.finish()
            let values = h.output.values
            expect(values.count == 12000,"Device reconnect changed the duration of buffered audio")
            equalSamples(Array(values.prefix(11280)),0.2,"Device reconnect altered healthy-source audio")
            equalSamples(Array(values.suffix(720)),0.5,"Device reconnect lost the previous device's final tail")
        }
        print("PASS: default-device reconnect preserves the old device tail and leaves its peer running")

        do {
            let h = Harness(mode:.microphone)
            let old = h.store.latest(.microphone)
            let late = old.onSamples
            old.tail = [Float](repeating:0.3,count:720)
            old.delayStop = true
            h.controller.wake()
            var stopped = false
            h.controller.stop { stopped = true }
            h.drain()
            expect(!stopped,"Stop completed before a pending reconnect had drained its old input")
            old.completeDelayedStop()
            wait(until:{stopped},"Stopping during reconnect never completed")
            expect(h.store.created[.microphone]?.count == 1,"Pending reconnect started a new input after stop")
            expect(h.output.values.count == 720,"Stopping during reconnect lost or duplicated the draining tail")
            equalSamples(h.output.values,0.3,"Pending reconnect tail was discarded during stop")
            late?([Float](repeating:0.9,count:12000),48000); h.drain()
            expect(h.output.values.count == 720,"Pending reconnect leaked samples after stop completion")
        }
        print("PASS: stop waits for in-flight reconnect drainage and prevents late restart or callbacks")
        print("Capture mode checks passed; no real audio inputs were opened")
    }
}
