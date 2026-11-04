#!/bin/sh
set -eu
cd "$(dirname "$0")"
appDestination="$HOME/Applications/Сводка встречи.app"
workerDestination="$HOME/Library/Application Support/MeetingOverlay/src"
python="${PYTHON:-runtime/.venv/bin/python}"
if [ -z "${MEETING_SIGN_IDENTITY:-}" ] && [ -f signing-identity.txt ]; then
    MEETING_SIGN_IDENTITY=$(cat signing-identity.txt)
    export MEETING_SIGN_IDENTITY
fi
# Installation is explicit: ad-hoc updates change the identity recognized by TCC.
if [ "${MEETING_BUILD_ONLY:-0}" != "1" ]; then
    if [ -z "${MEETING_SIGN_IDENTITY:-}" ] && [ "${MEETING_ALLOW_ADHOC:-0}" != "1" ]; then
        echo 'Установка требует MEETING_SIGN_IDENTITY; разовая ad-hoc установка — MEETING_ALLOW_ADHOC=1 (macOS может запросить разрешения заново).' >&2
        exit 2
    fi
    if pgrep -x MeetingOverlay >/dev/null; then
        echo 'Сначала завершите Сводку встречи: установленное приложение сейчас запущено.' >&2
        exit 3
    else
        process_status=$?
        if [ "$process_status" != "1" ]; then
            echo 'Не удалось проверить, закрыто ли приложение. Установка остановлена.' >&2
            exit 3
        fi
    fi
fi
build_directory=$(mktemp -d /private/tmp/meeting-build.XXXXXX)
trap 'rm -rf "$build_directory"' EXIT HUP INT TERM
bundle="$build_directory/Сводка встречи.app"
mkdir -p "$bundle/Contents/MacOS" "$bundle/Contents/Resources" .build.noindex
if [ "${MEETING_BUILD_ONLY:-0}" != "1" ] && [ -d "$appDestination" ]; then
    "$python" -B scripts/project_backup.py
fi
xcrun swiftc -swift-version 5 -O -module-cache-path /private/tmp/meeting-update-cache -framework AppKit -framework AVFoundation -framework CoreAudio src/MeetingOverlay.swift src/AppPaths.swift src/RuntimeBootstrap.swift src/SessionFiles.swift src/WorkerWatchdog.swift src/AudioTap.swift src/AudioSpool.swift src/MeetingRecording.swift src/WorkerClient.swift src/MicrophoneCapture.swift src/AudioMixer.swift src/CaptureController.swift src/MeetingIcon.swift src/SummaryFreshness.swift src/SummaryRequestPolicy.swift -o "$bundle/Contents/MacOS/MeetingOverlay"
/usr/libexec/PlistBuddy -c 'Clear dict' "$bundle/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Add :CFBundleExecutable string MeetingOverlay' -c 'Add :CFBundleIdentifier string local.ksandr.meeting-overlay' -c 'Add :CFBundleName string Сводка встречи' -c 'Add :CFBundleDisplayName string Сводка встречи' -c 'Add :CFBundlePackageType string APPL' -c 'Add :CFBundleShortVersionString string 0.7.0' -c 'Add :CFBundleVersion string 18' -c 'Add :CFBundleIconFile string MeetingSummary.icns' -c 'Add :LSUIElement bool true' -c 'Add :LSMinimumSystemVersion string 14.4' -c 'Add :NSMicrophoneUsageDescription string Выбранный микрофон обрабатывается локально для расшифровки и сводки встречи. Запись начинается только по вашей команде.' -c 'Add :NSAudioCaptureUsageDescription string Звук приложений обрабатывается локально для краткой сводки встречи. Изображение экрана не захватывается.' "$bundle/Contents/Info.plist"
cp tests/meeting-sample.wav "$bundle/Contents/Resources/meeting-sample.wav"
cp assets/MeetingOverlay.icns "$bundle/Contents/Resources/MeetingSummary.icns"
if [ "${MEETING_BUILD_ONLY:-0}" = "1" ]; then
    /usr/bin/ditto -c -k --keepParent --norsrc --noextattr "$bundle" .build.noindex/latest-build.tmp.zip
    /usr/bin/unzip -tq .build.noindex/latest-build.tmp.zip
    mv .build.noindex/latest-build.tmp.zip .build.noindex/latest-build.zip
    exit 0
fi
/usr/bin/codesign --force --sign "${MEETING_SIGN_IDENTITY:--}" --identifier local.ksandr.meeting-overlay "$bundle"
/usr/bin/codesign --verify --deep --strict "$bundle"
# The working writer contains unaccepted experiments. Native-only builds retain
# the deployed modules; intentional Python updates use a reviewed staging path.
worker_source="${MEETING_WORKER_SOURCE:-$workerDestination}"
# Installer requires distinct sources; copy the selected runtime modules to staging.
mkdir "$build_directory/workers"
for name in worker.py recent_transcript.py summary_writer.py transcript_log.py session_title.py; do
    cp "$worker_source/$name" "$build_directory/workers/$name"
done
"$python" -B scripts/install_release.py \
    "$bundle" "$appDestination" \
    "$build_directory/workers" "$workerDestination" \
    > .build.noindex/install-receipt.json
"$python" -B scripts/project_backup.py --verify-only --retire-installer-backups \
    > .build.noindex/maintenance-receipt.json
rm -f .build.noindex/latest-build.zip
/usr/bin/touch "$appDestination"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$appDestination"
