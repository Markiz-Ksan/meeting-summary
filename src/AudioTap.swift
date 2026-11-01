import Foundation
import CoreAudio

func check(_ status: OSStatus, _ operation: String) throws {
    if status != noErr {
        throw NSError(domain: "Audio", code: Int(status), userInfo: [NSLocalizedDescriptionKey: "\(operation): код \(status)"])
    }
}

/// Captures the system output before the speaker mute/volume controls.
/// Hardware changes are serialized separately from audio conversion and delivery.
final class AudioTap {
    private let state = CaptureState()

    var rate: Double { state.captureInfo.rate }
    var outputUID: String { state.captureInfo.uid }
    var onSamples: (([Float], Double) -> Void)? {
        get { state.onAudioQueue { state.handler } }
        set { state.onAudioQueue { state.handler = newValue } }
    }

    static func currentOutputUID() throws -> String {
        try CaptureState.outputUID(for: CaptureState.currentOutputDevice())
    }

    func start(completion: @escaping (Result<Void, Error>) -> Void) {
        let state = state
        state.control.async {
            let result = Result { try state.startSync() }
            DispatchQueue.main.async { completion(result) }
        }
    }

    /// The completion follows the final partial batch and all sample deliveries
    /// that the receiver has already submitted to the main queue.
    func stop(completion: (() -> Void)? = nil) {
        let state = state
        state.control.async {
            state.stopSync()
            if let completion { DispatchQueue.main.async(execute: completion) }
        }
    }

    deinit {
        // A callback can release the last wrapper reference. Never stop hardware
        // synchronously on its audio queue or capture a deinitializing wrapper.
        let state = state
        state.control.async { state.stopSync() }
    }

    private final class CaptureState {
        let queue = DispatchQueue(label: "local.meeting.audio", qos: .userInitiated)
        let control = DispatchQueue(label: "local.meeting.audio.control")
        private let queueKey = DispatchSpecificKey<UInt8>()
        private let infoLock = NSLock()
        private var info = (uid: "", rate: 48000.0)
        var captureInfo: (uid: String, rate: Double) {
            infoLock.lock(); defer { infoLock.unlock() }
            return info
        }

        // These fields are accessed only on the control queue.
        private var tap: AudioObjectID = 0
        private var device: AudioObjectID = 0
        private var io: AudioDeviceIOProcID?

        // These fields are accessed only on the audio queue.
        var handler: (([Float], Double) -> Void)?
        private var pending = [Float]()
        private var batchFrames = 12000
        private var sampleRate = 48000.0
        private var acceptingSamples = false

        init() { queue.setSpecific(key: queueKey, value: 1) }

        func onAudioQueue<T>(_ body: () throws -> T) rethrows -> T {
            if DispatchQueue.getSpecific(key: queueKey) != nil { return try body() }
            return try queue.sync(execute: body)
        }

        static func currentOutputDevice() throws -> AudioObjectID {
            var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                     mScope: kAudioObjectPropertyScopeGlobal,
                                                     mElement: kAudioObjectPropertyElementMain)
            var size = UInt32(MemoryLayout<AudioObjectID>.size)
            var outputDevice = AudioObjectID(0)
            try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &outputDevice), "Устройство вывода")
            guard outputDevice != kAudioObjectUnknown else {
                throw NSError(domain: "Audio", code: 3, userInfo: [NSLocalizedDescriptionKey: "Устройство вывода звука недоступно"])
            }
            return outputDevice
        }

        static func outputUID(for outputDevice: AudioObjectID) throws -> String {
            var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyDeviceUID,
                                                     mScope: kAudioObjectPropertyScopeGlobal,
                                                     mElement: kAudioObjectPropertyElementMain)
            var size = UInt32(MemoryLayout<CFString>.size)
            var uid: Unmanaged<CFString>?
            try check(AudioObjectGetPropertyData(outputDevice, &address, 0, nil, &size, &uid), "Идентификатор звука")
            guard let uid else {
                throw NSError(domain: "Audio", code: 3, userInfo: [NSLocalizedDescriptionKey: "Не удалось определить звуковой выход"])
            }
            return uid.takeRetainedValue() as String
        }

        func startSync() throws {
            stopSync()
            do {
                let outputDevice = try Self.currentOutputDevice()
                var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType,
                                                         mScope: kAudioObjectPropertyScopeGlobal,
                                                         mElement: kAudioObjectPropertyElementMain)
                var size = UInt32(MemoryLayout<UInt32>.size)
                var transport: UInt32 = 0
                try check(AudioObjectGetPropertyData(outputDevice, &address, 0, nil, &size, &transport), "Тип устройства")
                if transport == kAudioDeviceTransportTypeVirtual {
                    throw NSError(domain: "Audio", code: 4, userInfo: [NSLocalizedDescriptionKey: "Выбран виртуальный звуковой выход. Выберите динамики или наушники в настройках звука macOS."])
                }
                let uid = try Self.outputUID(for: outputDevice)
                let description = CATapDescription(excludingProcesses: [], deviceUID: uid, stream: 0)
                description.name = "Сводка встречи — звук Mac"
                description.isPrivate = true
                description.muteBehavior = .unmuted
                try check(AudioHardwareCreateProcessTap(description, &tap), "Доступ к звуку Mac")
                var format = AudioStreamBasicDescription()
                address.mSelector = kAudioTapPropertyFormat
                size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
                try check(AudioObjectGetPropertyData(tap, &address, 0, nil, &size, &format), "Формат звука")
                guard format.mFormatID == kAudioFormatLinearPCM,
                      format.mBitsPerChannel == 32,
                      format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
                      format.mSampleRate.isFinite, format.mSampleRate > 0 else {
                    throw NSError(domain: "Audio", code: 1, userInfo: [NSLocalizedDescriptionKey: "Неподдерживаемый формат аудиоустройства"])
                }
                infoLock.lock(); info = (uid, format.mSampleRate); infoLock.unlock()
                let composition: [String: Any] = [
                    kAudioAggregateDeviceNameKey: "Meeting Summary Private Audio",
                    kAudioAggregateDeviceUIDKey: UUID().uuidString,
                    kAudioAggregateDeviceIsPrivateKey: true,
                    kAudioAggregateDeviceTapAutoStartKey: false,
                    kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]]
                ]
                try check(AudioHardwareCreateAggregateDevice(composition as CFDictionary, &device), "Подключение звука")
                address.mSelector = kAudioDevicePropertyDeviceIsAlive
                var alive: UInt32 = 0
                size = UInt32(MemoryLayout<UInt32>.size)
                for _ in 0..<30 {
                    try check(AudioObjectGetPropertyData(device, &address, 0, nil, &size, &alive), "Готовность звука")
                    if alive != 0 { break }
                    Thread.sleep(forTimeInterval: 0.1)
                }
                guard alive != 0 else {
                    throw NSError(domain: "Audio", code: 2, userInfo: [NSLocalizedDescriptionKey: "Аудиоустройство не готово"])
                }
                onAudioQueue {
                    sampleRate = format.mSampleRate
                    batchFrames = max(1, Int(sampleRate * 0.25))
                    pending.removeAll(keepingCapacity: true)
                    pending.reserveCapacity(batchFrames * 2)
                    acceptingSamples = true
                }
                try check(AudioDeviceCreateIOProcIDWithBlock(&io, device, queue) { [weak self] _, input, _, _, _ in
                    self?.consume(input)
                }, "Захват аудио")
                try check(AudioDeviceStart(device, io), "Начало захвата")
            } catch {
                stopSync()
                throw error
            }
        }

        private func consume(_ input: UnsafePointer<AudioBufferList>) {
            guard acceptingSamples else { return }
            let buffers = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
            guard let first = buffers.first(where: { $0.mData != nil && $0.mDataByteSize > 0 && $0.mNumberChannels > 0 }) else { return }
            let frames = Int(first.mDataByteSize) / (MemoryLayout<Float>.size * Int(first.mNumberChannels))
            guard frames > 0 else { return }
            var mono = [Float](repeating: 0, count: frames)
            var channels = 0
            for buffer in buffers {
                guard let data = buffer.mData else { continue }
                let count = Int(buffer.mNumberChannels)
                guard count > 0, Int(buffer.mDataByteSize) >= frames * count * MemoryLayout<Float>.size else { continue }
                let floats = data.assumingMemoryBound(to: Float.self)
                for frame in 0..<frames {
                    for channel in 0..<count { mono[frame] += floats[frame * count + channel] }
                }
                channels += count
            }
            guard channels > 0 else { return }
            for index in mono.indices { mono[index] /= Float(channels) }
            pending.append(contentsOf: mono)
            while pending.count >= batchFrames {
                let batch = Array(pending.prefix(batchFrames))
                pending.removeFirst(batchFrames)
                handler?(batch, sampleRate)
            }
        }

        func stopSync() {
            if let io {
                AudioDeviceStop(device, io)
                AudioDeviceDestroyIOProcID(device, io)
                self.io = nil
            }
            // All previously submitted callbacks finish before the partial tail
            // is emitted. Late callbacks see acceptingSamples=false and exit.
            onAudioQueue {
                acceptingSamples = false
                if !pending.isEmpty {
                    let tail = pending
                    pending.removeAll(keepingCapacity: false)
                    handler?(tail, sampleRate)
                }
            }
            if device != 0 { AudioHardwareDestroyAggregateDevice(device); device = 0 }
            if tap != 0 { AudioHardwareDestroyProcessTap(tap); tap = 0 }
        }
    }
}
