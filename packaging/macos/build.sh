#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$ROOT"

if ! command -v uv >/dev/null; then
  echo "uv is required to build" >&2
  exit 1
fi
if [[ $(uname -s) != Darwin ]]; then
  echo "The macOS app must be built on macOS" >&2
  exit 1
fi

uv python install 3.12

if [[ -n ${ROBOCAP_FFMPEG_DIR:-} ]]; then
  FFMPEG_DIR=$ROBOCAP_FFMPEG_DIR
elif command -v brew >/dev/null && brew --prefix ffmpeg >/dev/null 2>&1; then
  FFMPEG_DIR=$(brew --prefix ffmpeg)/bin
else
  echo "Install FFmpeg with Homebrew or set ROBOCAP_FFMPEG_DIR" >&2
  exit 1
fi
for tool in ffmpeg ffprobe; do
  if [[ ! -x "$FFMPEG_DIR/$tool" ]]; then
    echo "Missing executable $FFMPEG_DIR/$tool" >&2
    exit 1
  fi
done
if ! "$FFMPEG_DIR/ffmpeg" -hide_banner -loglevel quiet -encoders | grep -q libx264; then
  echo "FFmpeg must include the libx264 encoder used by video repair" >&2
  exit 1
fi
export ROBOCAP_FFMPEG_DIR=$FFMPEG_DIR

uv sync --frozen --python 3.12 --extra desktop --extra build --group dev
uv run --python 3.12 pytest -q \
  tests/test_robocap_cloud_cli.py \
  tests/test_robocap_container_cli.py \
  tests/test_robocap_desktop_gui.py \
  tests/test_robocap_desktop_scanner.py \
  tests/test_robocap_desktop_validator.py \
  tests/test_robocap_desktop_conversion.py \
  tests/test_robocap_runtime.py

uv run --python 3.12 pyinstaller --noconfirm --clean packaging/macos/robocap_to_mcap.spec

APP="$ROOT/dist/RoboCapToMCAP.app"
test -d "$APP"
plutil -lint "$APP/Contents/Info.plist"

PACKAGED_FFMPEG=$(find "$APP/Contents" -type f -path '*/ffmpeg/bin/ffmpeg' -print -quit)
PACKAGED_FFPROBE=$(find "$APP/Contents" -type f -path '*/ffmpeg/bin/ffprobe' -print -quit)
test -n "$PACKAGED_FFMPEG"
test -n "$PACKAGED_FFPROBE"
"$PACKAGED_FFMPEG" -hide_banner -version >/dev/null
"$PACKAGED_FFPROBE" -hide_banner -version >/dev/null
"$PACKAGED_FFMPEG" -hide_banner -loglevel quiet -encoders | grep -q libx264

# This first release is ad-hoc signed. Replace '-' with an organizational
# Developer ID Application identity before external notarized distribution.
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

VERSION=$(uv run --python 3.12 python -c 'import robocap_to_mcap; print(robocap_to_mcap.__version__)')
ARCH=$(uname -m)
DMG_ROOT="$ROOT/dist/dmg-root"
DMG="$ROOT/dist/RoboCapToMCAP-${VERSION}-macos-${ARCH}-unsigned.dmg"
rm -rf "$DMG_ROOT" "$DMG"
mkdir -p "$DMG_ROOT"
ditto "$APP" "$DMG_ROOT/RoboCapToMCAP.app"
ln -s /Applications "$DMG_ROOT/Applications"
hdiutil create -volname "RoboCap to MCAP ${VERSION}" \
  -srcfolder "$DMG_ROOT" -ov -format UDZO "$DMG"
rm -rf "$DMG_ROOT"

shasum -a 256 "$DMG" | tee "$DMG.sha256"
echo "Built $DMG"
