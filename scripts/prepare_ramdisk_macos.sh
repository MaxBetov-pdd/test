#!/usr/bin/env bash
# Prepare an expanded SSH-injected APFS RestoreRamDisk on macOS.
#
# Usage:
#   bash scripts/prepare_ramdisk_macos.sh STOCK_DMG OUTPUT_DMG

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STOCK_DMG="${1:-}"
OUTPUT_DMG="${2:-}"
SSH_TAR="${3:-$ROOT/resources/ssh.tar.gz}"
RESTORED_EXTERNAL="${4:-$ROOT/resources/restored_external}"
GTAR_BIN="${GTAR_BIN:-$(command -v gtar || true)}"
MOUNT_POINT="${TMPDIR:-/tmp}/ICHPreparedRamdisk"

if [[ "$(uname)" != "Darwin" ]]; then
    echo "error: APFS preparation requires macOS hdiutil/diskutil" >&2
    exit 1
fi
if [[ -z "$STOCK_DMG" || -z "$OUTPUT_DMG" ]]; then
    echo "usage: $0 STOCK_DMG OUTPUT_DMG [SSH_TAR] [RESTORED_EXTERNAL]" >&2
    exit 64
fi
if [[ -z "$GTAR_BIN" ]]; then
    echo "error: GNU tar is required (brew install gnu-tar)" >&2
    exit 1
fi
for path in "$STOCK_DMG" "$SSH_TAR" "$RESTORED_EXTERNAL"; do
    [[ -s "$path" ]] || { echo "error: missing input: $path" >&2; exit 1; }
done

# shellcheck source=ramdisk_expand.sh
source "$ROOT/scripts/ramdisk_expand.sh"

detach_mount() {
    hdiutil detach -force "$MOUNT_POINT" >/dev/null 2>&1 || true
}
trap detach_mount EXIT

mkdir -p "$(dirname "$OUTPUT_DMG")"
cp "$STOCK_DMG" "$OUTPUT_DMG"

nr_expand_inject_ramdisk \
    "$OUTPUT_DMG" \
    "$SSH_TAR" \
    "$MOUNT_POINT" \
    "$GTAR_BIN"

detach_mount
mkdir -p "$MOUNT_POINT"
hdiutil attach \
    -mountpoint "$MOUNT_POINT" \
    -owners off \
    -imagekey diskimage-class=CRawDiskImage \
    "$OUTPUT_DMG" >/dev/null

RESTORED_DEST="$MOUNT_POINT/usr/local/bin/restored_external"
MOUNT_ICH_DEST="$MOUNT_POINT/usr/bin/mount_ich"
[[ -d "$(dirname "$RESTORED_DEST")" ]] || {
    echo "error: prepared ramdisk has no /usr/local/bin" >&2
    exit 1
}
[[ -f "$MOUNT_ICH_DEST" ]] || {
    echo "error: SSH payload did not install /usr/bin/mount_ich" >&2
    exit 1
}
cp "$RESTORED_EXTERNAL" "$RESTORED_DEST"
chmod 755 "$RESTORED_DEST" "$MOUNT_ICH_DEST"
sync
detach_mount
trap - EXIT

python3 - "$OUTPUT_DMG" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
size = path.stat().st_size
with path.open("rb") as handle:
    handle.seek(32)
    magic = handle.read(4)
if magic != b"NXSB":
    raise SystemExit(f"prepared image is not raw APFS: {path}")
if not 256 * 1024 * 1024 <= size <= 280 * 1024 * 1024:
    raise SystemExit(f"prepared image size is outside 256-280 MiB: {size}")
print(f"prepared APFS ramdisk: {path} ({size / 1024 / 1024:.1f} MiB)")
PY
