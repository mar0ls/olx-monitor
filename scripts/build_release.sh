#!/usr/bin/env bash

set -euo pipefail

if [ -n "${SOURCE_ROOT:-}" ]; then
  ROOT_DIR="$(cd "$SOURCE_ROOT" && pwd)"
else
  ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist/release}"
mkdir -p "$ROOT_DIR/build"
WORK_ROOT="$(mktemp -d "$ROOT_DIR/build/release.XXXXXX")"
TMP_DIST="$WORK_ROOT/dist"
TMP_BUILD="$WORK_ROOT/build"
TMP_SPEC="$WORK_ROOT/spec"
PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-$WORK_ROOT/pyinstaller-cache}"
export PYINSTALLER_CONFIG_DIR

cleanup() {
  if [ -d "$WORK_ROOT" ]; then
    rm -rf "$WORK_ROOT"
  fi
}

trap cleanup EXIT

mkdir -p "$DIST_DIR" "$TMP_DIST" "$TMP_BUILD" "$TMP_SPEC" "$PYINSTALLER_CONFIG_DIR"

resolve_python() {
  if [ -n "${PYTHON_BIN:-}" ]; then
    printf '%s\n' "$PYTHON_BIN"
    return
  fi

  if [ -x "$ROOT_DIR/venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/venv/bin/python"
    return
  fi

  if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    return
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi

  command -v python
}

PYTHON_CMD="$(resolve_python)"
OS_NAME="$(uname -s)"

build_with_pyinstaller() {
  "$PYTHON_CMD" -m PyInstaller "$@"
}

add_data_arg() {
  local source_path="$1"
  local target_dir="$2"

  if [[ "$OS_NAME" == MINGW* || "$OS_NAME" == MSYS* || "$OS_NAME" == CYGWIN* || "$OS_NAME" == Windows_NT ]]; then
    local win_source="$source_path"
    if command -v cygpath >/dev/null 2>&1; then
      win_source="$(cygpath -w "$source_path")"
    fi
    printf '%s;%s\n' "$win_source" "$target_dir"
  else
    printf '%s:%s\n' "$source_path" "$target_dir"
  fi
}

CHECKMARK_DATA="$(add_data_arg "$ROOT_DIR/assets/checkmark.svg" "assets")"
ICON_PNG_DATA="$(add_data_arg "$ROOT_DIR/assets/icon.png" "assets")"

case "$OS_NAME" in
  Darwin)
    # PyInstaller konwertuje PNG do ICNS przez Pillow. To omija wadliwy
    # iconutil z macOS 26, który odrzuca nawet własny poprawny iconset.
    build_with_pyinstaller \
      --noconfirm \
      --windowed \
      --name olx-monitor \
      --icon "$ROOT_DIR/assets/icon.png" \
      --add-data "$CHECKMARK_DATA" \
      --add-data "$ICON_PNG_DATA" \
      --hidden-import otodom_scraper \
      --hidden-import http_client \
      --hidden-import curl_cffi \
      --hidden-import curl_cffi.requests \
      --hidden-import curl_cffi._wrapper \
      --hidden-import _cffi_backend \
      --distpath "$TMP_DIST" \
      --workpath "$TMP_BUILD" \
      --specpath "$TMP_SPEC" \
      "$ROOT_DIR/olx_gui.py"

    ditto -c -k --sequesterRsrc --keepParent \
      "$TMP_DIST/olx-monitor.app" \
      "$DIST_DIR/olx-monitor-macos.zip"

    printf 'Built %s\n' "$DIST_DIR/olx-monitor-macos.zip"
    ;;
  Linux)
    build_with_pyinstaller \
      --noconfirm \
      --onefile \
      --name olx-monitor \
      --add-data "$CHECKMARK_DATA" \
      --add-data "$ICON_PNG_DATA" \
      --hidden-import otodom_scraper \
      --hidden-import http_client \
      --hidden-import curl_cffi \
      --hidden-import curl_cffi.requests \
      --hidden-import curl_cffi._wrapper \
      --hidden-import _cffi_backend \
      --distpath "$TMP_DIST" \
      --workpath "$TMP_BUILD" \
      --specpath "$TMP_SPEC" \
      "$ROOT_DIR/olx_gui.py"

    mv -f "$TMP_DIST/olx-monitor" "$DIST_DIR/olx-monitor-linux"
    printf 'Built %s\n' "$DIST_DIR/olx-monitor-linux"
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    # Pillow (instalowany w release.yml) konwertuje PNG do ICO.
    build_with_pyinstaller \
      --noconfirm \
      --onefile \
      --windowed \
      --name olx-monitor \
      --icon "$ROOT_DIR/assets/icon.png" \
      --add-data "$CHECKMARK_DATA" \
      --add-data "$ICON_PNG_DATA" \
      --hidden-import otodom_scraper \
      --hidden-import http_client \
      --hidden-import curl_cffi \
      --hidden-import curl_cffi.requests \
      --hidden-import curl_cffi._wrapper \
      --hidden-import _cffi_backend \
      --distpath "$TMP_DIST" \
      --workpath "$TMP_BUILD" \
      --specpath "$TMP_SPEC" \
      "$ROOT_DIR/olx_gui.py"

    mv -f "$TMP_DIST/olx-monitor.exe" "$DIST_DIR/olx-monitor-windows.exe"
    printf 'Built %s\n' "$DIST_DIR/olx-monitor-windows.exe"
    ;;
  *)
    printf 'Unsupported operating system: %s\n' "$OS_NAME" >&2
    exit 1
    ;;
esac
