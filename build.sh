#!/bin/sh
set -eu
cd "$(dirname "$0")"
app_destination="${MEETING_APP_DESTINATION:-$HOME/Applications/Сводка встречи.app}"
worker_destination="$HOME/Library/Application Support/MeetingOverlay/src"
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
mkdir -p 'Сводка встречи.app/Contents/MacOS' 'Сводка встречи.app/Contents/Resources' runtime/swift-cache
xcrun swiftc -swift-version 5 -O -module-cache-path runtime/swift-cache -framework AppKit -framework AVFoundation -framework CoreAudio src/MeetingOverlay.swift src/AudioTap.swift src/AudioSpool.swift src/WorkerClient.swift src/MicrophoneCapture.swift src/AudioMixer.swift src/CaptureController.swift src/MeetingIcon.swift src/SummaryFreshness.swift src/SummaryRequestPolicy.swift -o 'Сводка встречи.app/Contents/MacOS/MeetingOverlay'
/usr/libexec/PlistBuddy -c 'Clear dict' 'Сводка встречи.app/Contents/Info.plist'
/usr/libexec/PlistBuddy -c 'Add :CFBundleExecutable string MeetingOverlay' -c 'Add :CFBundleIdentifier string local.ksandr.meeting-overlay' -c 'Add :CFBundleName string Сводка встречи' -c 'Add :CFBundleDisplayName string Сводка встречи' -c 'Add :CFBundlePackageType string APPL' -c 'Add :CFBundleShortVersionString string 0.6.4' -c 'Add :CFBundleVersion string 11' -c 'Add :CFBundleIconFile string MeetingSummary.icns' -c 'Add :LSUIElement bool true' -c 'Add :LSMinimumSystemVersion string 14.4' -c 'Add :NSMicrophoneUsageDescription string Выбранный микрофон обрабатывается локально для расшифровки и сводки встречи. Запись начинается только по вашей команде.' -c 'Add :NSAudioCaptureUsageDescription string Звук приложений обрабатывается локально для краткой сводки встречи. Изображение экрана не захватывается.' 'Сводка встречи.app/Contents/Info.plist'
cp tests/meeting-sample.wav 'Сводка встречи.app/Contents/Resources/meeting-sample.wav'
cp assets/MeetingOverlay.icns 'Сводка встречи.app/Contents/Resources/MeetingSummary.icns'
if [ "${MEETING_BUILD_ONLY:-0}" = "1" ]; then exit 0; fi
# Sign a clean release outside the synced Documents folder.
staging=$(mktemp -d /private/tmp/meeting-release.XXXXXX)
trap 'rm -rf "$staging"' EXIT HUP INT TERM
/usr/bin/ditto --norsrc --noextattr 'Сводка встречи.app' "$staging/Сводка встречи.app"
/usr/bin/codesign --force --sign "${MEETING_SIGN_IDENTITY:--}" --identifier local.ksandr.meeting-overlay "$staging/Сводка встречи.app"
/usr/bin/codesign --verify --deep --strict "$staging/Сводка встречи.app"
# Replace the app and workers together; retain verified prior files for recovery.
# A failed final signature/hash check restores both destinations.
"${PYTHON:-python3}" -B scripts/install_release.py \
    "$staging/Сводка встречи.app" \
    "$app_destination" \
    src "$worker_destination"
# Refresh only this app's registration after its icon resources change.
/usr/bin/touch "$app_destination"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$app_destination"

