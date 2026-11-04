import Foundation
import Darwin

/// Owns one local Python worker and its pipes. The protocol and all pipe state
/// live on a serial queue; UI callbacks are delivered on the main queue.
final class WorkerClient {
    private let mode: String
    private let session: URL
    private let transcript: URL?
    private let resume: Bool
    private let audioFile: URL?
    private let audioStartedAt: Double?
    private let executableOverride: URL?
    private let scriptOverride: URL?
    private let queue = DispatchQueue(label: "local.meeting.worker-client", qos: .userInitiated)
    private let queueKey = DispatchSpecificKey<Bool>()
    private let bufferLimit = 2 * 1024 * 1024
    private var generation = UUID()
    private var process: Process?
    private var input: FileHandle?
    private var output: FileHandle?
    private var errorOutput: FileHandle?
    private var outputSource: DispatchSourceRead?
    private var errorSource: DispatchSourceRead?
    private var inputSource: DispatchSourceWrite?
    private var inputSourceSuspended = false
    private var outputBuffer = Data()
    private var inputBuffer = Data()
    private var inputOffset = 0
    private var discardingLine = false
    private var stderrTail = Data()
    private var stderrBytes: UInt64 = 0
    private var lastStderrDiagnostic = Date.distantPast
    private var protocolErrors = 0
    private var launchTime = Date()
    private var eventCallback: (([String: Any]) -> Void)?
    private var exitCallback: ((Int32) -> Void)?

    var onEvent: (([String: Any]) -> Void)? {
        get { synchronized { eventCallback } }
        set { synchronized { eventCallback = newValue } }
    }
    var onExit: ((Int32) -> Void)? {
        get { synchronized { exitCallback } }
        set { synchronized { exitCallback = newValue } }
    }
    var startedAt: Date {
        get { synchronized { launchTime } }
        set { synchronized { launchTime = newValue } }
    }
    var isRunning: Bool { synchronized { process?.isRunning == true } }

    init(mode: String, session: URL, transcript: URL?, resume: Bool = false,
         audioFile: URL? = nil, audioStartedAt: Double? = nil,
         executableURL: URL? = nil, scriptURL: URL? = nil) {
        self.mode = mode
        self.session = session
        self.transcript = transcript
        self.resume = resume
        self.audioFile = audioFile
        self.audioStartedAt = audioStartedAt
        self.executableOverride = executableURL
        self.scriptOverride = scriptURL
        queue.setSpecific(key: queueKey, value: true)
    }

    deinit { stop() }

    private func synchronized<T>(_ action: () throws -> T) rethrows -> T {
        if DispatchQueue.getSpecific(key: queueKey) == true { return try action() }
        return try queue.sync(execute: action)
    }

    private func failure(_ code: Int, _ text: String) -> NSError {
        NSError(domain: "MeetingWorker", code: code, userInfo: [NSLocalizedDescriptionKey: text])
    }

    func start() throws {
        try synchronized {
            guard mode == "transcribe" || mode == "summarize" else {
                throw failure(1, "Неизвестный режим обработки")
            }
            guard process == nil else { throw failure(2, "Обработка уже запущена") }
            generation = UUID()
            let token = generation
            outputBuffer.removeAll(keepingCapacity: true)
            inputBuffer.removeAll(keepingCapacity: true)
            inputOffset = 0
            discardingLine = false
            stderrTail.removeAll(keepingCapacity: true)
            stderrBytes = 0
            lastStderrDiagnostic = .distantPast
            protocolErrors = 0

            let child = Process()
            child.executableURL = executableOverride ?? AppPaths.python
            var arguments = ["-u", (scriptOverride ?? AppPaths.workerScript).path,
                             "--mode", mode, "--session", session.path]
            if mode == "transcribe", let transcript {
                arguments += ["--transcript-file", transcript.path]
                if let audioFile, let audioStartedAt {
                    arguments += ["--audio-file", audioFile.path, "--audio-start", String(audioStartedAt)]
                }
            }
            if resume { arguments.append("--resume") }
            child.arguments = arguments
            child.environment = AppPaths.workerEnvironment

            let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
            child.standardInput = stdin
            child.standardOutput = stdout
            child.standardError = stderr
            input = stdin.fileHandleForWriting
            output = stdout.fileHandleForReading
            errorOutput = stderr.fileHandleForReading
            process = child
            child.terminationHandler = { [weak self] exited in
                guard let self else { return }
                self.queue.async { [weak self] in
                    self?.finished(exited, token: token)
                }
            }
            do {
                for handle in [input, output, errorOutput].compactMap({ $0 }) {
                    let flags = fcntl(handle.fileDescriptor, F_GETFL)
                    guard flags >= 0, fcntl(handle.fileDescriptor, F_SETFL, flags | O_NONBLOCK) == 0 else {
                        throw failure(3, "Не удалось настроить канал обработки (код \(errno))")
                    }
                }
                // A closed worker stdin must produce EPIPE, never kill the app.
                guard fcntl(stdin.fileHandleForWriting.fileDescriptor, F_SETNOSIGPIPE, 1) == 0 else {
                    throw failure(4, "Не удалось защитить канал обработки (код \(errno))")
                }
                try child.run()
                launchTime = Date()
                try? stdin.fileHandleForReading.close()
                try? stdout.fileHandleForWriting.close()
                try? stderr.fileHandleForWriting.close()
                outputSource = reader(for: stdout.fileHandleForReading, token: token, isError: false)
                errorSource = reader(for: stderr.fileHandleForReading, token: token, isError: true)
                let writeHandle = stdin.fileHandleForWriting
                let source = DispatchSource.makeWriteSource(fileDescriptor: writeHandle.fileDescriptor, queue: queue)
                source.setEventHandler { [weak self] in
                    guard let self, self.generation == token else { return }
                    do { try self.flushInput(token: token) }
                    catch { self.protocolError("stdin_write_failed", bytes: 0, token: token) }
                }
                source.setCancelHandler { try? writeHandle.close() }
                inputSource = source
                inputSourceSuspended = true
            } catch {
                child.terminationHandler = nil
                closePipes()
                try? stdin.fileHandleForReading.close()
                try? stdout.fileHandleForWriting.close()
                try? stderr.fileHandleForWriting.close()
                if child.isRunning { terminate(child) }
                process = nil
                throw error
            }
        }
    }

    /// Accepts a bounded JSON request without blocking the UI on a busy worker.
    func send(_ request: [String: Any]) throws {
        guard JSONSerialization.isValidJSONObject(request) else {
            throw failure(5, "Некорректный запрос обработки")
        }
        var bytes = try JSONSerialization.data(withJSONObject: request)
        bytes.append(10)
        try synchronized {
            guard process?.isRunning == true, input != nil else {
                throw failure(6, "Обработка не запущена")
            }
            let pending = inputBuffer.count - inputOffset
            guard bytes.count <= bufferLimit, pending + bytes.count <= bufferLimit else {
                throw failure(7, "Очередь запросов обработки переполнена")
            }
            if inputOffset > 0 {
                inputBuffer.removeFirst(inputOffset)
                inputOffset = 0
            }
            inputBuffer.append(bytes)
            try flushInput(token: generation)
        }
    }

    private func flushInput(token: UUID) throws {
        guard generation == token, let input else { return }
        while inputOffset < inputBuffer.count {
            let written = inputBuffer.withUnsafeBytes { bytes in
                Darwin.write(input.fileDescriptor, bytes.baseAddress!.advanced(by: inputOffset), bytes.count - inputOffset)
            }
            if written > 0 { inputOffset += written; continue }
            if written < 0, errno == EINTR { continue }
            if written < 0, errno == EAGAIN || errno == EWOULDBLOCK {
                if inputSourceSuspended {
                    inputSourceSuspended = false
                    inputSource?.resume()
                }
                return
            }
            let code = errno
            closeInput()
            inputBuffer.removeAll(keepingCapacity: true)
            inputOffset = 0
            throw failure(8, "Канал обработки закрылся (код \(code))")
        }
        inputBuffer.removeAll(keepingCapacity: true)
        inputOffset = 0
        if !inputSourceSuspended, let inputSource {
            inputSource.suspend()
            inputSourceSuspended = true
        }
    }

    private func reader(for handle: FileHandle, token: UUID, isError: Bool) -> DispatchSourceRead {
        let source = DispatchSource.makeReadSource(fileDescriptor: handle.fileDescriptor, queue: queue)
        source.setEventHandler { [weak self] in
            guard let self, self.generation == token else { return }
            self.drain(token: token, isError: isError)
        }
        source.setCancelHandler { try? handle.close() }
        source.resume()
        return source
    }

    private func drain(token: UUID, isError: Bool) {
        guard generation == token, let handle = isError ? errorOutput : output else { return }
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        // A noisy child must not monopolise this queue and delay stop().
        for _ in 0..<32 {
            let count = Darwin.read(handle.fileDescriptor, &buffer, buffer.count)
            if count > 0 {
                let bytes = Data(buffer.prefix(count))
                if isError {
                    stderrBytes += UInt64(count)
                    stderrTail.append(bytes.suffix(8192))
                    if stderrTail.count > 8192 { stderrTail.removeFirst(stderrTail.count - 8192) }
                    if Date().timeIntervalSince(lastStderrDiagnostic) >= 5 {
                        lastStderrDiagnostic = Date()
                        stderrDiagnostic(token: token)
                    }
                } else { consume(bytes, token: token) }
                continue
            }
            if count < 0, errno == EINTR { continue }
            if count < 0, errno == EAGAIN || errno == EWOULDBLOCK { return }
            if count < 0 { protocolError(isError ? "stderr_read_failed" : "stdout_read_failed", bytes: 0, token: token) }
            if isError {
                errorSource?.cancel(); errorSource = nil
                errorOutput = nil
            } else {
                outputSource?.cancel(); outputSource = nil
                output = nil
                if !discardingLine, !outputBuffer.isEmpty { parseLine(outputBuffer, token: token) }
                outputBuffer.removeAll(keepingCapacity: true)
                discardingLine = false
            }
            return
        }
    }

    private func consume(_ bytes: Data, token: UUID) {
        var cursor = bytes.startIndex
        while cursor < bytes.endIndex {
            let newline = bytes[cursor...].firstIndex(of: 10)
            let end = newline ?? bytes.endIndex
            let segment = bytes[cursor..<end]
            if !discardingLine {
                if outputBuffer.count + segment.count > bufferLimit {
                    protocolError("stdout_line_too_large", bytes: outputBuffer.count + segment.count, token: token)
                    outputBuffer.removeAll(keepingCapacity: true)
                    discardingLine = true
                } else { outputBuffer.append(segment) }
            }
            if let newline {
                if !discardingLine { parseLine(outputBuffer, token: token) }
                outputBuffer.removeAll(keepingCapacity: true)
                discardingLine = false
                cursor = bytes.index(after: newline)
            } else { break }
        }
    }

    private func parseLine(_ bytes: Data, token: UUID) {
        guard !bytes.isEmpty else { return }
        guard let object = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any],
              let event = object["event"] as? String, !event.isEmpty else {
            protocolError("unexpected_stdout", bytes: bytes.count, token: token)
            return
        }
        emit(object, token: token)
    }

    private func protocolError(_ reason: String, bytes: Int, token: UUID) {
        protocolErrors += 1
        // Report malformed output without retaining or exposing speech. Rate
        // limiting prevents a broken worker from flooding the main queue.
        guard protocolErrors <= 5 || protocolErrors % 100 == 0 else { return }
        emit(["event": "protocol_error", "mode": mode, "reason": reason,
              "bytes": bytes, "count": protocolErrors], token: token)
    }

    private func stderrDiagnostic(token: UUID) {
        emit(["event": "diagnostic", "kind": "worker_stderr", "mode": mode,
              "bytes": stderrBytes, "tailBytes": stderrTail.count], token: token)
    }

    private func emit(_ event: [String: Any], token: UUID) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let callback: (([String: Any]) -> Void)? = self.synchronized {
                self.generation == token ? self.eventCallback : nil
            }
            callback?(event)
        }
    }

    private func finished(_ child: Process, token: UUID) {
        guard generation == token, process === child else { return }
        // Process exit can be observed before the final readable pipe event.
        drain(token: token, isError: false)
        drain(token: token, isError: true)
        if stderrBytes > 0 { stderrDiagnostic(token: token) }
        closePipes()
        child.terminationHandler = nil
        process = nil
        let status = child.terminationStatus
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            let callback: ((Int32) -> Void)? = self.synchronized {
                self.generation == token ? self.exitCallback : nil
            }
            callback?(status)
        }
    }

    func stop() {
        synchronized {
            generation = UUID()
            eventCallback = nil
            exitCallback = nil
            let child = process
            process = nil
            child?.terminationHandler = nil
            closePipes()
            if let child, child.isRunning { terminate(child) }
        }
    }

    private func closePipes() {
        closeInput()
        if let outputSource { outputSource.cancel() }
        else { try? output?.close() }
        if let errorSource { errorSource.cancel() }
        else { try? errorOutput?.close() }
        outputSource = nil
        errorSource = nil
        output = nil
        errorOutput = nil
        outputBuffer.removeAll(keepingCapacity: false)
        inputBuffer.removeAll(keepingCapacity: false)
        inputOffset = 0
        stderrTail.removeAll(keepingCapacity: false)
    }

    private func closeInput() {
        if let source = inputSource {
            // A cancelled source must be resumed for its cancellation handler
            // to run. The handler closes the FD after libdispatch releases it.
            if inputSourceSuspended { source.resume() }
            source.cancel()
        } else { try? input?.close() }
        inputSource = nil
        inputSourceSuspended = false
        input = nil
    }

    private func terminate(_ child: Process) {
        child.terminate()
        let pid = child.processIdentifier
        // Retain this exact Process, never look up a worker by name or kill a
        // subsequently launched replacement. This also survives client deinit.
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 3) {
            if child.isRunning, child.processIdentifier == pid {
                _ = Darwin.kill(pid, SIGKILL)
            }
        }
    }
}
