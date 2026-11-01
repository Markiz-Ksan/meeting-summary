import Foundation
import AVFoundation
import CoreMedia

private func microphoneError(_ code: Int, _ message: String) -> NSError {
    NSError(domain: "MeetingMicrophone", code: code, userInfo: [NSLocalizedDescriptionKey: message])
}

/// Converts native capture PCM without assuming its bit depth or channel layout.
/// Conversion keeps the hardware sample rate; the ASR worker handles resampling.
final class MicrophonePCMDecoder {
    private var converter: AVAudioConverter?
    private var inputFormat: AVAudioFormat?

    func decode(_ sampleBuffer: CMSampleBuffer) throws -> (samples: [Float], rate: Double) {
        guard CMSampleBufferDataIsReady(sampleBuffer),
              let description = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(description),
              asbd.pointee.mFormatID == kAudioFormatLinearPCM,
              let format = AVAudioFormat(streamDescription: asbd) else {
            throw microphoneError(20, "Микрофон передал неподдерживаемый звуковой формат")
        }
        let count = CMSampleBufferGetNumSamples(sampleBuffer)
        guard count > 0, count <= Int(Int32.max),
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(count)) else {
            throw microphoneError(21, "Не удалось прочитать аудиофрагмент микрофона")
        }
        buffer.frameLength = AVAudioFrameCount(count)
        let result = CMSampleBufferCopyPCMDataIntoAudioBufferList(sampleBuffer, at: 0,
                                                                 frameCount: Int32(count), into: buffer.mutableAudioBufferList)
        guard result == noErr else {
            throw microphoneError(Int(result), "Не удалось скопировать звук микрофона: код \(result)")
        }
        return try convert(buffer)
    }

    func convert(_ input: AVAudioPCMBuffer) throws -> (samples: [Float], rate: Double) {
        let format = input.format
        guard format.sampleRate.isFinite, format.sampleRate > 0, format.channelCount > 0 else {
            throw microphoneError(22, "Микрофон передал неверную частоту или число каналов")
        }
        guard input.frameLength > 0 else { return ([], format.sampleRate) }
        let floatBuffer: AVAudioPCMBuffer
        if format.commonFormat == .pcmFormatFloat32 && !format.isInterleaved {
            floatBuffer = input
        } else {
            if inputFormat != format {
                let destination = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: format.sampleRate,
                                                channels: format.channelCount, interleaved: false)!
                converter = AVAudioConverter(from: format, to: destination)
                inputFormat = format
            }
            guard let converter,
                  let output = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: input.frameLength) else {
                throw microphoneError(23, "Не удалось преобразовать формат микрофона")
            }
            try converter.convert(to: output, from: input)
            floatBuffer = output
        }
        guard let channels = floatBuffer.floatChannelData else {
            throw microphoneError(24, "В аудиофрагменте микрофона нет звуковых данных")
        }
        let count = Int(floatBuffer.frameLength), channelCount = Int(floatBuffer.format.channelCount)
        var mono = [Float](repeating: 0, count: count)
        for channel in 0..<channelCount {
            for index in 0..<count { mono[index] += channels[channel][index] }
        }
        let divisor = Float(channelCount)
        for index in mono.indices {
            let value = mono[index] / divisor
            mono[index] = value.isFinite ? value : 0
        }
        return (mono, format.sampleRate)
    }
}

/// Independent microphone input. No audio is played back and the system input
/// device, input volume and microphone mute settings are never modified.
final class MicrophoneCapture {
    private let state: CaptureState

    init(deviceUID: String? = nil) { state = CaptureState(requestedUID: deviceUID) }

    var deviceUID: String { state.currentUID }
    var onSamples: (([Float], Double) -> Void)? {
        get { state.onAudioQueue { state.sampleHandler } }
        set { state.onAudioQueue { state.sampleHandler = newValue } }
    }
    /// Runtime errors arrive on the main queue after the microphone is stopped.
    var onError: ((Error) -> Void)? {
        get { state.onAudioQueue { state.errorHandler } }
        set { state.onAudioQueue { state.errorHandler = newValue } }
    }

    static func devices() -> [(uid: String, name: String)] {
        let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external],
                                                         mediaType: .audio, position: .unspecified)
        var seen = Set<String>()
        return discovery.devices.filter { $0.isConnected && seen.insert($0.uniqueID).inserted }
            .map { (uid: $0.uniqueID, name: $0.localizedName) }
    }

    static func currentDefaultUID() throws -> String {
        guard let device = AVCaptureDevice.default(for: .audio), device.isConnected else {
            throw microphoneError(1, "Микрофон не найден. Подключите микрофон или наушники с микрофоном.")
        }
        return device.uniqueID
    }

    func start(completion: @escaping (Result<Void, Error>) -> Void) {
        let state = state
        state.control.async { state.start(completion: completion) }
    }

    func stop(completion: (() -> Void)? = nil) {
        let state = state
        state.control.async {
            state.stopSync()
            if let completion { DispatchQueue.main.async(execute: completion) }
        }
    }

    deinit {
        let state = state
        state.control.async { state.stopSync() }
    }

    private final class CaptureState: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
        let control = DispatchQueue(label: "local.meeting.microphone.control")
        private let audio = DispatchQueue(label: "local.meeting.microphone.audio", qos: .userInitiated)
        private let queueKey = DispatchSpecificKey<UInt8>()
        private let uidLock = NSLock()
        private var activeUID = ""
        let requestedUID: String?
        var currentUID: String {
            uidLock.lock(); defer { uidLock.unlock() }
            return activeUID
        }

        // Control-queue state.
        private var session: AVCaptureSession?
        private var output: AVCaptureAudioDataOutput?
        private var observers = [NSObjectProtocol]()
        private var generation = UUID()
        private var startCompletion: ((Result<Void, Error>) -> Void)?

        // Audio-queue state.
        var sampleHandler: (([Float], Double) -> Void)?
        var errorHandler: ((Error) -> Void)?
        private var decoder = MicrophonePCMDecoder()
        private var pending = [Float]()
        private var sampleRate = 0.0
        private var audioGeneration = UUID()
        private var acceptingSamples = false
        private var conversionFailed = false

        init(requestedUID: String?) {
            self.requestedUID = requestedUID
            super.init()
            audio.setSpecific(key: queueKey, value: 1)
        }

        func onAudioQueue<T>(_ body: () throws -> T) rethrows -> T {
            if DispatchQueue.getSpecific(key: queueKey) != nil { return try body() }
            return try audio.sync(execute: body)
        }

        func start(completion: @escaping (Result<Void, Error>) -> Void) {
            stopSync()
            startCompletion = completion
            let token = generation
            switch AVCaptureDevice.authorizationStatus(for: .audio) {
            case .authorized: configure(token: token)
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .audio) { [weak self] allowed in
                    guard let self else { return }
                    self.control.async {
                        guard self.generation == token else { return }
                        if allowed { self.configure(token: token) }
                        else { self.finishStart(.failure(Self.permissionError())) }
                    }
                }
            case .denied, .restricted: finishStart(.failure(Self.permissionError()))
            @unknown default: finishStart(.failure(Self.permissionError()))
            }
        }

        private static func permissionError() -> NSError {
            microphoneError(2, "Нет доступа к микрофону. Разрешите «Сводке встречи» доступ в настройках macOS → Конфиденциальность и безопасность → Микрофон.")
        }

        private func finishStart(_ result: Result<Void, Error>) {
            let completion = startCompletion
            startCompletion = nil
            if let completion { DispatchQueue.main.async { completion(result) } }
        }

        private func configure(token: UUID) {
            guard generation == token else { return }
            do {
                let microphone: AVCaptureDevice?
                if let requestedUID {
                    microphone = AVCaptureDevice(uniqueID: requestedUID)
                } else { microphone = AVCaptureDevice.default(for: .audio) }
                guard let microphone, microphone.isConnected, microphone.hasMediaType(.audio) else {
                    throw microphoneError(3, "Выбранный микрофон отключён или недоступен. Подключите его или выберите другой микрофон.")
                }
                let input = try AVCaptureDeviceInput(device: microphone)
                let session = AVCaptureSession(), output = AVCaptureAudioDataOutput()
                // Request uncompressed Float32; the decoder also accepts integer
                // and interleaved PCM if a device supplies another native layout.
                output.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM,
                                        AVLinearPCMIsFloatKey: true, AVLinearPCMBitDepthKey: 32,
                                        AVLinearPCMIsBigEndianKey: false, AVLinearPCMIsNonInterleaved: false]
                session.beginConfiguration()
                guard session.canAddInput(input), session.canAddOutput(output) else {
                    session.commitConfiguration()
                    throw microphoneError(4, "Не удалось подключить выбранный микрофон к записи")
                }
                session.addInput(input); session.addOutput(output)
                session.commitConfiguration()
                uidLock.lock(); activeUID = microphone.uniqueID; uidLock.unlock()
                self.session = session; self.output = output
                onAudioQueue {
                    pending.removeAll(keepingCapacity: false)
                    sampleRate = 0; decoder = MicrophonePCMDecoder()
                    audioGeneration = token
                    conversionFailed = false; acceptingSamples = true
                }
                output.setSampleBufferDelegate(self, queue: audio)
                observers.append(NotificationCenter.default.addObserver(forName: AVCaptureSession.runtimeErrorNotification,
                                                                         object: session, queue: nil) { [weak self] notification in
                    let error = notification.userInfo?[AVCaptureSessionErrorKey] as? Error
                        ?? microphoneError(5, "Захват микрофона прервался")
                    self?.report(error, token: token)
                })
                observers.append(NotificationCenter.default.addObserver(forName: AVCaptureDevice.wasDisconnectedNotification,
                                                                         object: nil, queue: nil) { [weak self] notification in
                    guard let disconnected = notification.object as? AVCaptureDevice,
                          disconnected.uniqueID == microphone.uniqueID else { return }
                    self?.report(microphoneError(6, "Микрофон «\(microphone.localizedName)» отключён"), token: token)
                })
                session.startRunning()
                guard session.isRunning else { throw microphoneError(7, "Микрофон не начал запись. Проверьте его подключение и разрешение macOS.") }
                finishStart(.success(()))
            } catch {
                let completion = startCompletion; startCompletion = nil
                stopSync()
                if let completion { DispatchQueue.main.async { completion(.failure(error)) } }
            }
        }

        private func report(_ error: Error, token: UUID) {
            control.async { [weak self] in
                guard let self, self.generation == token else { return }
                let handler = self.onAudioQueue { self.errorHandler }
                self.stopSync()
                if let handler { DispatchQueue.main.async { handler(error) } }
            }
        }

        func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
            guard acceptingSamples, !conversionFailed else { return }
            do {
                let converted = try decoder.decode(sampleBuffer)
                if converted.rate != sampleRate {
                    flushTail()
                    sampleRate = converted.rate
                }
                pending.append(contentsOf: converted.samples)
                let batchFrames = max(1, Int(sampleRate * 0.25))
                while pending.count >= batchFrames {
                    let batch = Array(pending.prefix(batchFrames))
                    pending.removeFirst(batchFrames)
                    sampleHandler?(batch, sampleRate)
                }
            } catch {
                conversionFailed = true
                report(error, token: audioGeneration)
            }
        }

        private func flushTail() {
            guard !pending.isEmpty, sampleRate > 0 else { return }
            let tail = pending
            pending.removeAll(keepingCapacity: false)
            sampleHandler?(tail, sampleRate)
        }

        func stopSync() {
            generation = UUID()
            finishStart(.failure(microphoneError(8, "Запуск микрофона отменён")))
            for observer in observers { NotificationCenter.default.removeObserver(observer) }
            observers.removeAll()
            session?.stopRunning()
            output?.setSampleBufferDelegate(nil, queue: nil)
            // StopRunning plus the serial barrier makes every accepted sample,
            // including the partial final batch, precede stop's main completion.
            onAudioQueue { acceptingSamples = false; flushTail() }
            output = nil; session = nil
        }
    }
}
