#!/bin/sh
# sshrd_provision.sh - everything that has to happen from SSHRD, in one pass.
#
# Run on the HOST with the device booted into SSHRD. It stages payloads over
# ssh and runs the device-side work, then verifies. Every step is idempotent:
# re-running is safe and already-done steps are skipped, so this can be used
# both for a fresh restore and to top up a device after a partial run.
#
# The System volume is read-only on a normal boot, which is the only reason any
# of this needs SSHRD at all. Group it here so one trip does the lot.
#
#   ./sshrd_provision.sh              # everything
#   ./sshrd_provision.sh --list       # show the steps
#   ./sshrd_provision.sh resolv apps  # only the named steps
#   ./sshrd_provision.sh --check      # verify only, change nothing
#
# Nothing is deleted. Replaced files keep a .orig; replaced directories a .prev.

set -e
BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE"

# Host-side helper binaries. Resolved from $BASE rather than the working
# directory so the script works when invoked by an absolute path. Where tools/
# sits is the only thing that differs between the repo and research copies, so
# it is resolved once here rather than spelled out at each call site.
TOOLS="$BASE/../tools"

SSHPASS="$TOOLS/sshpass"
SSHOPT="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=25 -p 2222"
DEV="root@localhost"
PW=alpine

IPSW_ROOT=/tmp/ios27-rootfs           # decrypted root filesystem, mounted
STAGE=/mnt2/_provision                # device-side staging, Data volume

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '    [+] %s\n' "$1"; }
skip() { printf '    [=] %s\n' "$1"; }
die()  { printf '    [!] %s\n' "$1"; exit 1; }

# shellcheck disable=SC2086  # $SSHOPT is an option LIST and must word-split
sh_dev() { timeout 900 "$SSHPASS" -p "$PW" ssh $SSHOPT "$DEV" "$@"; }

# Single-quoted bodies passed to must_dev/sh_dev are deliberate: the variables
# inside them belong to the DEVICE and must not be expanded on the host. Linters
# will flag those quotes as a mistake; they are not. Run the linter with that
# check suppressed rather than "fixing" the quoting.
#
# Run device-side work and require it to print DONE_OK as its last act.
# ssh's exit status is not trustworthy enough on its own here: it reports the
# transport result, and `set -e` does not propagate out of $(...) or pipelines.
# An explicit marker is unambiguous.
must_dev() {
    _out=$(sh_dev "$1" 2>&1) || true
    if ! printf '%s' "$_out" | grep -q DONE_OK; then
        printf '%s\n' "$_out" | sed 's/^/        /'
        die "${2:-device step failed}"
    fi
    printf '%s' "$_out" | grep -v DONE_OK | sed 's/^/        /' || true
}

# Copy a file to the device and verify the byte count landed.
put() {
    [ -f "$1" ] || die "missing local file: $1"
    _sz=$(wc -c < "$1" | tr -d ' ')
    # shellcheck disable=SC2086  # same: $SSHOPT must word-split
    timeout 900 "$SSHPASS" -p "$PW" ssh $SSHOPT "$DEV" "cat > $2" < "$1" \
        || die "transfer failed: $1 -> $2"
    # tr runs on the HOST here (output of ssh), so it is fine
    _got=$(sh_dev "wc -c < $2" 2>/dev/null | tr -d ' \r')
    [ "$_got" = "$_sz" ] || die "short write: $2 is $_got bytes, expected $_sz"
}

STEPS="mounts cache jbtools sileo resolv apps verify"

usage() {
    echo "steps: $STEPS"
    echo "  mounts   mount System rw and Data (always runs first)"
    echo "  cache    deploy the launchd service cache (dropbear + jbboot)"
    echo "  jbtools  install jbboot.sh, personaalloc, photodiag into /var/jb"
    echo "  sileo      install Sileo (from payload/, built by fetch_payloads.sh)"
    # TrollStore is not installed here. It goes on after first boot from a deb,
    # so it can be updated without another DFU trip. See COMMANDS.md.
    echo "  resolv   write /private/etc/resolv.conf so CLI DNS works"
    echo "  apps     copy the 50 removable system apps into /Applications"
    echo "  verify   check the end state"
    exit 0
}

CHECK_ONLY=0
case "$1" in
    --list|-h|--help) usage ;;
    --check) CHECK_ONLY=1; WANT="mounts verify" ;;
    "") WANT="$STEPS" ;;
    *) WANT="mounts $* verify" ;;
esac

wants() { case " $WANT " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# ---------------------------------------------------------------- preflight
say "preflight"
[ -x "$SSHPASS" ] || die "sshpass not found at $SSHPASS"
sh_dev 'exit 0' >/dev/null 2>&1 || die "cannot reach the device on port 2222 (is iproxy running?)"

sh_dev '/sbin/mount | /usr/bin/grep -q "md0 on /"' \
    || die "device is NOT in SSHRD. Refusing; this would write to the wrong place."
ok "device is in SSHRD"

# ------------------------------------------------------------------ mounts
say "mounts"
must_dev '
mkdir -p /mnt1 /mnt2 2>/dev/null
/sbin/mount_apfs /dev/disk1s1 /mnt1 2>/dev/null
/sbin/mount_apfs /dev/disk1s2 /mnt2 2>/dev/null
/sbin/mount -u -o rw /dev/disk1s1 2>/dev/null
[ -d /mnt1/Applications ] || { echo "System volume not mounted"; exit 1; }
[ -d /mnt2/jb ] || echo "warning: /mnt2/jb absent, bootstrap not installed?"
touch /mnt1/Applications/.wtest 2>/dev/null || { echo "System volume NOT writable"; exit 1; }
rm -f /mnt1/Applications/.wtest
echo DONE_OK
' "could not mount System read-write"
ok "System rw, Data mounted"
sh_dev "mkdir -p $STAGE" >/dev/null

# ------------------------------------------------------------------- cache
if wants cache && [ "$CHECK_ONLY" = 0 ]; then
    say "launchd service cache"
    CACHE=boot/work/launchd.plist
    if [ ! -f "$CACHE" ]; then
        skip "no patched cache at $CACHE; build it with patch_launchd_cache.py + add_jbboot.py"
    else
        n=$(python3 -c "import plistlib,sys;print(len(plistlib.load(open('$CACHE','rb'))['LaunchDaemons']))")
        for j in com.dropbear com.jbboot; do
            python3 -c "
import plistlib,sys
d=plistlib.load(open('$CACHE','rb'))['LaunchDaemons']
sys.exit(0 if '/System/Library/LaunchDaemons/$j.plist' in d else 1)" \
                || die "$CACHE is missing the $j job; build it before deploying"
        done
        ok "cache has $n daemons including com.dropbear and com.jbboot"
        put "$CACHE" "$STAGE/launchd.plist"
        want=$(wc -c < "$CACHE" | tr -d ' ')
        must_dev "
T=/mnt1/System/Library/xpc/launchd.plist
[ -f \$T ] || { echo 'service cache missing on device'; exit 1; }
[ -f \$T.orig ] || cp \$T \$T.orig
cat $STAGE/launchd.plist > \$T
rm -f $STAGE/launchd.plist
set -- \$(wc -c < \$T); got=\$1
[ \"\$got\" = \"$want\" ] || { echo \"deployed cache is \$got bytes, expected $want\"; exit 1; }
[ -f \$T.sig ] || echo 'warning: detached .sig absent, launchd may reject the cache'
echo DONE_OK
" "failed to deploy the service cache"
        ok "deployed, $want bytes (the detached .sig is left in place deliberately)"
    fi
fi

# ----------------------------------------------------------------- jbtools
if wants jbtools && [ "$CHECK_ONLY" = 0 ]; then
    say "per-boot tools into /var/jb"
    sh_dev "mkdir -p /mnt2/jb/bin /mnt2/jb/usr/bin" >/dev/null
    if [ -f boot/jbboot.sh ]; then
        put boot/jbboot.sh /mnt2/jb/bin/jbboot.sh
        sh_dev 'chmod 755 /mnt2/jb/bin/jbboot.sh'
        ok "jbboot.sh"
    fi
    for b in spawnprobe/personaalloc photodiag/photodiag spawnprobe/personaprobe; do
        [ -f "$b" ] || continue
        n=$(basename "$b")
        put "$b" "/mnt2/jb/usr/bin/$n"
        sh_dev "chmod 755 /mnt2/jb/usr/bin/$n"
        ok "$n"
    done
fi

# ----------------------------------------------------------------- bundles
# Sileo comes from payload/, built by fetch_payloads.sh from the upstream
# release. It used to be scraped off the device (installed via dpkg, then pulled
# back and re-signed), which made provisioning depend on the device already being
# half-configured and meant a fresh clone could not reproduce it.
#
# TrollStore used to be installed here too. It is not any more: anything placed
# in /Applications lives on the sealed System volume and cannot be updated
# without another DFU trip, and the build that went here was the full TrollStore
# renamed to TrollStoreLite.app, whose helper cannot register apps on iOS 27.
# It now installs after first boot from a deb. See COMMANDS.md.
#
# Sent as a tar with ownership baked in, because the ramdisk has no chown, and
# extracted device-side as root so modes survive. giveMeRoot's setuid bit is the
# one that matters: without it Sileo cannot escalate and installs fail silently.
for pair in "Sileo.app:sileo"; do
    bundle=${pair%%:*}; step=${pair#*:}
    wants "$step" || continue
    [ "$CHECK_ONLY" = 0 ] || continue
    say "$bundle"
    if [ ! -d "payload/$bundle" ]; then
        skip "payload/$bundle absent; run ./fetch_payloads.sh first"
        continue
    fi
    TARB="payload/.work/$bundle.tar.gz"
    mkdir -p payload/.work
    "$TOOLS/gtar" czf "$TARB" --owner=0 --group=80 --numeric-owner --no-xattrs \
        -C payload "$bundle"
    timeout 900 "$SSHPASS" -p "$PW" ssh $SSHOPT "$DEV" \
        "cd /mnt1/Applications && tar xzf - --numeric-owner" < "$TARB" \
        || die "failed to extract $bundle on the device"
    must_dev "
B=/mnt1/Applications/$bundle
[ -d \$B ] || { echo '$bundle missing after extract'; exit 1; }
if [ -f \$B/giveMeRoot ]; then
    chmod 4755 \$B/giveMeRoot
    [ -u \$B/giveMeRoot ] || { echo 'giveMeRoot LOST its setuid bit'; exit 1; }
fi
echo DONE_OK
" "$bundle did not land correctly"
    ok "$bundle deployed"
done

# ------------------------------------------------------------------ resolv
if wants resolv && [ "$CHECK_ONLY" = 0 ]; then
    say "resolv.conf"
    # -s not -e: a failed earlier run once left a 0-byte file that an
    # exists-check happily skipped, and an empty resolv.conf is no better
    # than none. printf not heredoc: the ramdisk root is read-only, no /tmp.
    must_dev '
R=/mnt1/private/etc/resolv.conf
if [ -s "$R" ]; then
  echo "already present, leaving alone"
else
  printf "%s\n" \
    "# Added so command-line tools (apt) can resolve names." \
    "# iOS system APIs use mDNSResponder and do not need this." \
    "nameserver 1.1.1.1" "nameserver 8.8.8.8" "nameserver 8.8.4.4" > "$R"
  chmod 644 "$R"
  echo "wrote $R"
fi
# -s not -e: a failed run once left a 0-byte file that an exists-check skipped,
# and an empty resolv.conf is no better than none.
[ -s "$R" ] || { echo "resolv.conf is empty after write"; exit 1; }
echo DONE_OK
' "could not write resolv.conf"
    ok "resolv.conf in place"
fi

# -------------------------------------------------------------------- apps
if wants apps && [ "$CHECK_ONLY" = 0 ]; then
    say "system apps"
    # Check for actual bundles rather than a count threshold: a device could
    # have 260+ bundles and still be missing ours.
    missing=$(sh_dev '
n=0
for a in Camera MobileSafari Calculator Maps Photos; do
    [ -d "/mnt1/Applications/$a.app" ] || n=$((n+1))
done
echo $n' | tr -d ' \r')
    have=$(sh_dev 'ls /mnt1/Applications | wc -l' | tr -d ' \r')
    if [ "$missing" = 0 ]; then
        skip "the system apps are already present ($have bundles), skipping the 659 MB copy"
    elif [ ! -d "$IPSW_ROOT/private/var/staged_system_apps" ]; then
        skip "IPSW not mounted at $IPSW_ROOT; cannot source the apps"
    else
        SRC="$IPSW_ROOT/private/var/staged_system_apps"
        TAR=payload/apps50.tar.gz
        if [ ! -f "$TAR" ]; then
            ok "building $TAR (659 MB, about a minute)"
            mkdir -p payload
            # -C "$SRC" . rather than a word-split $(ls): archives the whole
            # directory without relying on app names being space-free.
            # Entries come out as ./Calculator.app/..., which extracts correctly.
            "$TOOLS/gtar" czf "$TAR" --owner=0 --group=80 --numeric-owner \
                --no-xattrs --mode='g+w' -C "$SRC" .
        fi
        ok "streaming $(du -h "$TAR" | cut -f1) to /Applications"
        timeout 1800 "$SSHPASS" -p "$PW" ssh $SSHOPT "$DEV" \
            'cd /mnt1/Applications && tar xzf - --numeric-owner' < "$TAR"
        ok "extracted"
    fi
fi

# ------------------------------------------------------------------ verify
say "verify"
FAIL=0
note() { printf '    %-24s %s\n' "$1" "$2"; case "$2" in ABSENT|MISSING|*LOST*) FAIL=1 ;; esac; }

note "/Applications bundles"  "$(sh_dev 'ls /mnt1/Applications | wc -l' | tr -d ' \r')"
for b in Sileo.app; do
    note "$b" "$(sh_dev "[ -d /mnt1/Applications/$b ] && echo present || echo ABSENT" | tr -d '\r')"
done
note "giveMeRoot setuid" "$(sh_dev '[ -u /mnt1/Applications/Sileo.app/giveMeRoot ] && echo OK || echo LOST' | tr -d '\r')"
note "resolv.conf bytes"  "$(sh_dev '[ -s /mnt1/private/etc/resolv.conf ] && wc -c < /mnt1/private/etc/resolv.conf || echo MISSING' | tr -d ' \r')"
note "jbboot.sh"          "$(sh_dev '[ -x /mnt2/jb/bin/jbboot.sh ] && echo present || echo ABSENT' | tr -d '\r')"
note "personaalloc"       "$(sh_dev '[ -x /mnt2/jb/usr/bin/personaalloc ] && echo present || echo ABSENT' | tr -d '\r')"

# The cache is the one file that can be the right SIZE and still be wrong, and
# it is what provides SSH on a normal boot. Pull it back and check its contents.
sh_dev '/bin/cat /mnt1/System/Library/xpc/launchd.plist' > payload/.work/dev_cache.plist 2>/dev/null || true
if [ -s payload/.work/dev_cache.plist ]; then
    cache_state=$(python3 - <<'PY'
import plistlib
try:
    ld = plistlib.load(open("payload/.work/dev_cache.plist","rb"))["LaunchDaemons"]
except Exception as e:
    print(f"UNREADABLE ({e})"); raise SystemExit
need = ["com.dropbear", "com.jbboot"]
missing = [j for j in need if f"/System/Library/LaunchDaemons/{j}.plist" not in ld]
print(f"{len(ld)} daemons, " + ("all jobs present" if not missing else f"MISSING {missing}"))
PY
)
    note "launchd cache" "$cache_state"
else
    note "launchd cache" "MISSING"
fi

sh_dev 'sync; sync' >/dev/null 2>&1 || true

if [ "$FAIL" != 0 ]; then
    printf '\n\033[1m==> INCOMPLETE\033[0m\n'
    echo "    something above is ABSENT or LOST. Do not assume a good boot."
    exit 1
fi

say "done, safe to reboot"
echo "    next: pwn DFU, then  ./get_boot.py && ./boot.py"
echo "    then on the device:  uicache -a -f"
