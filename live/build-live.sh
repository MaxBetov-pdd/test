#!/usr/bin/env bash
set -Eeuo pipefail

LIVE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$LIVE_ROOT/.." && pwd)"
WORK="$LIVE_ROOT/work"
DIST="$LIVE_ROOT/dist"

case "$WORK" in
    "$LIVE_ROOT"/work) ;;
    *) echo "Refusing unsafe work directory: $WORK" >&2; exit 1 ;;
esac

if ! command -v lb >/dev/null; then
    echo "live-build is missing (Debian/Ubuntu package: live-build)" >&2
    exit 1
fi

rm -rf -- "$WORK"
mkdir -p "$WORK" "$DIST"
rm -f -- "$DIST/ich-se2-live-amd64.iso" "$DIST/ich-se2-live-amd64.iso.sha256"
cd "$WORK"

lb config noauto \
    --mode debian \
    --distribution bookworm \
    --architectures amd64 \
    --binary-images iso-hybrid \
    --debian-installer none \
    --archive-areas "main contrib non-free-firmware" \
    --security false \
    --mirror-bootstrap "http://deb.debian.org/debian" \
    --mirror-chroot "http://deb.debian.org/debian" \
    --mirror-binary "http://deb.debian.org/debian" \
    --apt-recommends true \
    --iso-application "ICH A12 A13 SSH Ramdisk Live" \
    --iso-publisher "MaxBetov-pdd/test" \
    --iso-volume "ICH_SE2_LIVE" \
    --bootappend-live "boot=live components username=ich hostname=ich-live locales=ru_RU.UTF-8 keyboard-layouts=us,ru keyboard-options=grp:alt_shift_toggle"

cp -a "$LIVE_ROOT/overlay/config/." "$WORK/config/"
mkdir -p "$WORK/config/includes.chroot/opt/ich"
rsync -a \
    --exclude '/.git/' \
    --exclude '/.venv/' \
    --exclude '/.venv-linux/' \
    --exclude '/cache/' \
    --exclude '/work/' \
    --exclude '/live/work/' \
    --exclude '/live/dist/' \
    --exclude '/boot-console.log' \
    "$REPO_ROOT/" "$WORK/config/includes.chroot/opt/ich/"

chmod 0755 \
    "$WORK/config/hooks/live/010-ich.hook.chroot" \
    "$WORK/config/includes.chroot/usr/local/bin/ich-welcome" \
    "$WORK/config/includes.chroot/opt/ich/linux/ich" \
    "$WORK/config/includes.chroot/opt/ich/linux/setup.sh"

lb build

ISO_SOURCE="$WORK/live-image-amd64.hybrid.iso"
ISO_OUTPUT="$DIST/ich-se2-live-amd64.iso"
test -f "$ISO_SOURCE"
cp -f "$ISO_SOURCE" "$ISO_OUTPUT"
sha256sum "$ISO_OUTPUT" > "$ISO_OUTPUT.sha256"
echo "ISO: $ISO_OUTPUT"
