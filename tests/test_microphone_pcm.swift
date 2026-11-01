import Foundation
import AVFoundation
import CoreMedia

private struct TestFailure: Error { let message: String }
private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw TestFailure(message: message) }
}

private func sampleBuffer(_ pcm: AVAudioPCMBuffer) throws -> CMSampleBuffer {
    var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: Int32(pcm.format.sampleRate)),
                                    presentationTimeStamp: .zero, decodeTimeStamp: .invalid)
    var sample: CMSampleBuffer?
    let created = CMSampleBufferCreateReady(allocator: kCFAllocatorDefault, dataBuffer: nil,
                                           formatDescription: pcm.format.formatDescription,
                                           sampleCount: Int(pcm.frameLength), sampleTimingEntryCount: 1,
                                           sampleTimingArray: &timing, sampleSizeEntryCount: 0,
                                           sampleSizeArray: nil, sampleBufferOut: &sample)
    try require(created == noErr && sample != nil, "Cannot create synthetic CMSampleBuffer")
    let copied = CMSampleBufferSetDataBufferFromAudioBufferList(sample!, blockBufferAllocator: kCFAllocatorDefault,
                                                               blockBufferMemoryAllocator: kCFAllocatorDefault,
                                                               flags: 0, bufferList: pcm.audioBufferList)
    try require(copied == noErr, "Cannot populate synthetic CMSampleBuffer")
    return sample!
}

@main private struct MicrophonePCMTests {
    static func main() throws {
        let decoder = MicrophonePCMDecoder()
        let cases: [(AVAudioCommonFormat, Bool, AVAudioChannelCount, Double)] = [
            (.pcmFormatFloat32, false, 2, 48_000),
            (.pcmFormatFloat32, true, 2, 44_100),
            (.pcmFormatInt16, false, 2, 48_000),
            (.pcmFormatInt16, true, 2, 44_100),
            (.pcmFormatInt32, true, 2, 48_000),
            (.pcmFormatInt16, false, 1, 16_000)
        ]
        for (common, interleaved, channels, rate) in cases {
            let format = AVAudioFormat(commonFormat: common, sampleRate: rate, channels: channels, interleaved: interleaved)!
            let pcm = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 1024)!
            pcm.frameLength = 1024
            var expected = [Float]()
            for frame in 0..<1024 {
                let first = Float(frame % 4) * 0.5 - 0.75
                expected.append(channels == 1 ? first : first * 0.75)
                for channel in 0..<Int(channels) {
                    let value = channel == 0 ? first : first / 2
                    let plane = interleaved ? 0 : channel
                    let index = interleaved ? frame * Int(channels) + channel : frame
                    switch common {
                    case .pcmFormatFloat32: pcm.floatChannelData![plane][index] = value
                    case .pcmFormatInt16: pcm.int16ChannelData![plane][index] = Int16(value * 32768)
                    case .pcmFormatInt32: pcm.int32ChannelData![plane][index] = Int32(Double(value) * 2147483648)
                    default: fatalError("Unsupported test format")
                    }
                }
            }
            let decoded = try decoder.decode(sampleBuffer(pcm))
            try require(decoded.rate == rate, "Native sample rate changed")
            try require(decoded.samples.count == expected.count, "PCM conversion lost frames")
            for (actual, expected) in zip(decoded.samples, expected) {
                try require(abs(actual - expected) < 0.000001, "Incorrect PCM conversion/downmix")
            }
            print("PASS format=\(common.rawValue) interleaved=\(interleaved) channels=\(channels) rate=\(Int(rate))")
        }
        let format = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
        let invalid = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 3)!
        invalid.frameLength = 3
        invalid.floatChannelData![0][0] = .nan
        invalid.floatChannelData![0][1] = .infinity
        invalid.floatChannelData![0][2] = 0.25
        let finite = try decoder.decode(sampleBuffer(invalid))
        try require(finite.samples == [0, 0, 0.25], "Nonfinite samples were not sanitized")
        print("PASS nonfinite input is sanitized")

        // Lifecycle stop is safe before a start and does not request permission.
        let capture = MicrophoneCapture()
        var stopped = false
        capture.stop { stopped = true }
        let deadline = Date().addingTimeInterval(3)
        while !stopped && Date() < deadline { RunLoop.main.run(until: Date().addingTimeInterval(0.01)) }
        try require(stopped && capture.deviceUID.isEmpty, "Idle stop did not complete")
        print("PASS idle stop without hardware capture")
        print("PASS all 8 microphone tests; no microphone access requested")
    }
}
