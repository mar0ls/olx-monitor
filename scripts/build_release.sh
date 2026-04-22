#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist/release}"
mkdir -p "$ROOT_DIR/build"
WORK_ROOT="$(mktemp -d "$ROOT_DIR/build/release.XXXXXX")"
TMP_DIST="$WORK_ROOT/dist"
TMP_BUILD="$WORK_ROOT/build"
TMP_SPEC="$WORK_ROOT/spec"

cleanup() {
  if [ -d "$WORK_ROOT" ]; then
    rm -rf "$WORK_ROOT"
  fi
}

trap cleanup EXIT

mkdir -p "$DIST_DIR" "$TMP_DIST" "$TMP_BUILD" "$TMP_SPEC"

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

ensure_ico() {
  if [ -f "$ROOT_DIR/icon.ico" ]; then
    return
  fi

  "$PYTHON_CMD" -c "from pathlib import Path; import sys; from PIL import Image; root = Path(sys.argv[1]); Image.open(root / 'assets' / 'icon.png').save(root / 'icon.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])" "$ROOT_DIR"
}

ensure_icns() {
  if [ -f "$ROOT_DIR/assets/icon.icns" ]; then
    return
  fi

  local iconset_dir="$WORK_ROOT/icon.iconset"
  mkdir -p "$iconset_dir"

  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$ROOT_DIR/assets/icon.png" --out "$iconset_dir/icon_${size}x${size}.png" >/dev/null
    sips -z "$((size * 2))" "$((size * 2))" "$ROOT_DIR/assets/icon.png" --out "$iconset_dir/icon_${size}x${size}@2x.png" >/dev/null
  done

  iconutil -c icns "$iconset_dir" -o "$ROOT_DIR/assets/icon.icns"
}

build_with_pyinstaller() {
  "$PYTHON_CMD" -m PyInstaller "$@"
}

add_data_arg() {
  local source_path="$1"
  local target_dir="$2"

  if [[ "$OS_NAME" == MINGW* || "$OS_NAME" == MSYS* || "$OS_NAME" == CYGWIN* || "$OS_NAME" == Windows_NT ]]; then
    printf '%s;%s\n' "$source_path" "$target_dir"
  else
    printf '%s:%s\n' "$source_path" "$target_dir"
  fi
}

CHECKMARK_DATA="$(add_data_arg "$ROOT_DIR/assets/checkmark.svg" "assets")"
ICON_PNG_DATA="$(add_data_arg "$ROOT_DIR/assets/icon.png" "assets")"

case "$OS_NAME" in
  Darwin)
    ensure_icns
    build_with_pyinstaller \
      --noconfirm \
      --windowed \
      --name olx-monitor \
      --icon "$ROOT_DIR/assets/icon.icns" \
      --add-data "$CHECKMARK_DATA" \
      --add-data "$ICON_PNG_DATA" \
      --hidden-import otodom_scraper \
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
    ensure_ico
    build_with_pyinstaller \
      --noconfirm \
      --onefile \
      --name olx-monitor \
      --icon "$ROOT_DIR/icon.ico" \
      --add-data "$CHECKMARK_DATA" \
      --add-data "$ICON_PNG_DATA" \
      --hidden-import otodom_scraper \
      --distpath "$TMP_DIST" \
      --workpath "$TMP_BUILD" \
      --specpath "$TMP_SPEC" \
      "$ROOT_DIR/olx_gui.py"

    mv -f "$TMP_DIST/olx-monitor" "$DIST_DIR/olx-monitor-linux"
    printf 'Built %s\n' "$DIST_DIR/olx-monitor-linux"
    ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    ensure_ico
    build_with_pyinstaller \
      --noconfirm \
      --onefile \
      --windowed \
      --name olx-monitor \
      --icon "$ROOT_DIR/icon.ico" \
      --add-data "$CHECKMARK_DATA" \
      --add-data "$ICON_PNG_DATA" \
      --hidden-import otodom_scraper \
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
