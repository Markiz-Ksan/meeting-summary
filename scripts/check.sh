#!/bin/sh
# Short regression checks only: no models, capture devices or audio playback.
set -eu
cd "$(dirname "$0")/.."
scratch=$(mktemp -d /private/tmp/meeting-checks.XXXXXX)
trap 'rm -rf "$scratch"' EXIT HUP INT TERM
python="${PYTHON:-runtime/.venv/bin/python}"
"$python" -B -m unittest discover -s tests -p 'test_*.py'
compile() {
    name=$1
    shift
    xcrun swiftc -module-cache-path /private/tmp/meeting-update-cache "$@" -o "$scratch/$name"
    "$scratch/$name"
}
compile freshness src/SummaryFreshness.swift tests/test_summary_freshness.swift
compile summary_request src/SummaryRequestPolicy.swift tests/test_summary_request_policy.swift
compile capture -framework AVFoundation -framework CoreAudio src/AudioTap.swift src/MicrophoneCapture.swift src/AudioMixer.swift src/CaptureController.swift tests/test_capture_modes.swift
compile mixer -framework AVFoundation src/AudioMixer.swift tests/test_audio_mixer.swift
compile pcm -framework AVFoundation src/MicrophoneCapture.swift tests/test_microphone_pcm.swift
compile recording -framework AVFoundation src/MeetingRecording.swift tests/test_meeting_recording.swift
compile spool -framework AVFoundation src/MeetingRecording.swift src/AudioSpool.swift tests/test_audio_spool.swift
compile worker src/AppPaths.swift src/WorkerClient.swift tests/WorkerClientChecks.swift

compile watchdog src/WorkerWatchdog.swift tests/test_worker_watchdog.swift
compile recovery src/AppPaths.swift src/WorkerWatchdog.swift src/WorkerClient.swift tests/WorkerRecoveryChecks.swift
compile session_files src/SessionFiles.swift tests/test_session_files.swift
