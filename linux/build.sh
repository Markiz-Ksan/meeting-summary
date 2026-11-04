#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { echo 'Build on Linux x86_64'; exit 1; }
PYTHON="${PYTHON:-python3}"
VERSION="$($PYTHON linux/entrypoint.py --version)"
OUTPUT="${OUTPUT:-$PWD/.build.noindex/linux}"
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
mkdir -p "$OUTPUT"
"$PYTHON" linux/bundle_native.py "$BUILD_DIR/native"
"$PYTHON" -m PyInstaller --noconfirm --clean --onedir --name meeting-summary \
  --distpath "$BUILD_DIR/dist" --workpath "$BUILD_DIR/work" --specpath "$BUILD_DIR" \
  --paths "$PWD/linux" --paths "$PWD/src" \
  --hidden-import summary_writer --hidden-import session_title \
  --hidden-import recent_transcript --hidden-import transcript_log \
  --collect-all sherpa_onnx --collect-all llama_cpp --collect-data certifi \
  --copy-metadata PySide6-Essentials --copy-metadata shiboken6 \
  --recursive-copy-metadata sherpa-onnx --recursive-copy-metadata llama-cpp-python \
  --copy-metadata certifi \
  --add-data "$PWD/linux/meeting_summary/models.json:meeting_summary" \
  --add-data "$BUILD_DIR/native:native" \
  linux/entrypoint.py
APP="$BUILD_DIR/dist/meeting-summary"
cp linux/README.md linux/THIRD_PARTY.md "$APP/"
# Keep shared LGPL Qt libraries replaceable in a folder distribution.
"$APP/meeting-summary" --version
QT_QPA_PLATFORM=offscreen "$APP/meeting-summary" --gui-smoke --report "$OUTPUT/frozen-gui.json"
if [[ "${RUN_INFERENCE_TEST:-0}" == 1 ]]; then
  "$APP/meeting-summary" --inference-test tests/meeting-sample.wav --report "$OUTPUT/frozen-inference.json"
fi
DEB="$BUILD_DIR/deb"
mkdir -p "$DEB/DEBIAN" "$DEB/opt/meeting-summary" "$DEB/usr/bin" "$DEB/usr/share/applications" "$DEB/usr/share/icons/hicolor/scalable/apps"
cp -a "$APP/." "$DEB/opt/meeting-summary/"
ln -s /opt/meeting-summary/meeting-summary "$DEB/usr/bin/meeting-summary"
cp linux/meeting-summary.desktop "$DEB/usr/share/applications/"
cp linux/meeting-summary.svg "$DEB/usr/share/icons/hicolor/scalable/apps/"
cat > "$DEB/DEBIAN/control" <<EOF
Package: meeting-summary
Version: $VERSION
Section: sound
Priority: optional
Architecture: amd64
Maintainer: Meeting Summary contributors
Depends: libc6 (>= 2.35), libstdc++6, libgl1, libegl1, libxkbcommon0, libxcb-cursor0, libxcb1, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-render-util0, libxcb-xinerama0, libxcb-xkb1, libxkbcommon-x11-0
Recommends: pipewire-pulse | pulseaudio, xdg-user-dirs
Description: Local speech recognition and meeting summary overlay
 Bundles Python, Qt, CPU inference engines and audio tools.
 Models are downloaded with pinned SHA-256 validation on first start.
EOF
dpkg-deb --root-owner-group --build "$DEB" "$OUTPUT/meeting-summary_${VERSION}_amd64.deb"
APPDIR="$BUILD_DIR/MeetingSummary.AppDir"
mkdir -p "$APPDIR/usr/lib/meeting-summary"
cp -a "$APP/." "$APPDIR/usr/lib/meeting-summary/"
cp linux/meeting-summary.desktop linux/meeting-summary.svg "$APPDIR/"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
APPDIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
exec "$APPDIR/usr/lib/meeting-summary/meeting-summary" "$@"
EOF
chmod +x "$APPDIR/AppRun"
# Fixed tool digest prevents mutable continuous releases from silently changing the builder.
TOOL="$BUILD_DIR/appimagetool"
curl --fail --location --retry 3 'https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage' -o "$TOOL"
echo '62aebc77c2149e96b593b94da9bbd1881e0984a56399ff6d7a03b7ddd7c9ce68  '"$TOOL" | sha256sum --check
chmod +x "$TOOL"
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" "$APPDIR" "$OUTPUT/MeetingSummary-${VERSION}-x86_64.AppImage"
cd "$OUTPUT"
sha256sum ./*.deb ./*.AppImage > SHA256SUMS
