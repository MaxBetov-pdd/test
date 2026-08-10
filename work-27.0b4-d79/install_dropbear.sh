#!/bin/zsh
#
# Install dropbear onto the device's System volume. RUN FROM THE MAC, with the device
# booted into SSHRD.
#
#   ./install_dropbear.sh            install
#   ./install_dropbear.sh --check    verify only, change nothing
#
# Source is ssh.tar.gz here, byte-identical to upstream's work-27.0b3/ssh.tar.gz. We only ever
# hand-placed a few files, and one of them was wrong: the host keys went in as
# `rsa_host_key` etc, but dropbear's compiled-in defaults are `dropbear_rsa_host_key`, so
# it would never have found them.
#
# Deliberately minimal. dropbear links only libz.1.dylib and libSystem.B.dylib, both
# already on the device, so nothing else is required to run it. The package also carries
# bin/{sh,ls,cat,bash,...} built in 2022; those are NOT installed here. Overwriting /bin/sh
# on the System volume risks the whole boot and buys nothing for SSH.
#
# The binary is NOT re-signed. It ships signed with `platform-application` and
# `com.apple.private.security.container-required=false`, and an ldid re-sign would strip
# the restricted entitlement. Loading it relies on the AMFIIsCDHashInTrustCache kernel
# patch, same as everything else we run.

set -u
cd "${0:A:h}"

PKG=ssh.tar.gz
STAGE=${TMPDIR:-/tmp}/dropbear-stage-$$
SSHPASS=../tools/sshpass

# ssh takes -p <port>, scp takes -P <port>. Keeping them as separate arrays avoids the
# obvious trap: `-p 2222` inside one array is TWO elements, so trying to rewrite it with a
# string substitution silently does nothing and scp ends up with -p (preserve times) and
# no port at all, connecting to localhost:22.
COMMON=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
        -o LogLevel=ERROR -o ConnectTimeout=8)
SSH_OPTS=("${COMMON[@]}" -p 2222)
SCP_OPTS=("${COMMON[@]}" -P 2222)

CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

# Left = path inside the tarball, right = path on the device. /mnt1 is the System volume.
FILES=(
  "usr/local/bin/dropbear|/mnt1/usr/local/bin/dropbear"
  "usr/local/bin/dropbearkey|/mnt1/usr/local/bin/dropbearkey"
  "private/etc/dropbear/dropbear_rsa_host_key|/mnt1/private/etc/dropbear/dropbear_rsa_host_key"
  "private/etc/dropbear/dropbear_ecdsa_host_key|/mnt1/private/etc/dropbear/dropbear_ecdsa_host_key"
  "private/etc/dropbear/dropbear_dss_host_key|/mnt1/private/etc/dropbear/dropbear_dss_host_key"
)

# ---------------------------------------------------------------- preflight
[[ -f "$PKG" ]]     || { print -u2 "[!] missing $PKG"; exit 1; }
[[ -x "$SSHPASS" ]] || { print -u2 "[!] missing $SSHPASS"; exit 1; }

sshdev() { "$SSHPASS" -p alpine ssh "${SSH_OPTS[@]}" root@localhost "$@"; }

pkill -f "^iproxy 2222" 2>/dev/null
iproxy 2222 22 >/dev/null 2>&1 &
IPROXY_PID=$!
trap 'kill $IPROXY_PID 2>/dev/null' EXIT
# iproxy needs a moment to bind, otherwise the first connect fails for reasons that have
# nothing to do with the device.
sleep 2

sshdev true 2>/dev/null || { print -u2 "[!] no SSH on 2222. Is the device booted into SSHRD?"; exit 1; }

# Refuse to run against a normally-booted device. /mnt1 only exists under SSHRD, and this
# check is upstream's own (COMMANDS.md section 3).
WHERE=$(sshdev "/sbin/mount | grep -q 'md0 on /' && echo SSHRD || echo NORMAL" 2>/dev/null)
if [[ "$WHERE" != "SSHRD" ]]; then
    print -u2 "[!] device reports '${WHERE:-unknown}', not SSHRD."
    print -u2 "    Refusing: /mnt1 only exists on the ramdisk, so this would write nothing useful."
    exit 1
fi
print "[*] device is in SSHRD"

sshdev "/sbin/mount | grep -q ' /mnt1 ' || /sbin/mount_apfs /dev/disk1s1 /mnt1" >/dev/null 2>&1
sshdev "/sbin/mount | grep -q ' /mnt1 '" || { print -u2 "[!] /mnt1 not mounted"; exit 1; }
print "[*] /mnt1 mounted"

# The ramdisk has no mkdir, so both target directories must already exist. They do, from an
# earlier session, but check rather than assume: a missing dir makes scp fail with a message
# that does not say why.
for d in /mnt1/usr/local/bin /mnt1/private/etc/dropbear; do
    sshdev "[ -d '$d' ]" || {
        print -u2 "[!] $d does not exist on the device, and the ramdisk has no mkdir."
        print -u2 "    Create it from a shell that has one before re-running."
        exit 1
    }
done
print "[*] target directories present"

# ---------------------------------------------------------------- stage
mkdir -p "$STAGE" || exit 1
tar xzf "$PKG" -C "$STAGE" || { print -u2 "[!] extract failed"; exit 1; }
print "[*] extracted $PKG -> $STAGE"

for e in "${FILES[@]}"; do
    src="${e%%|*}"
    [[ -f "$STAGE/$src" ]] || { print -u2 "[!] not in the tarball: $src"; exit 1; }
done

# report <label> compares on-device size against the staged source
report() {
    local src="$1" dst="$2" want got line perm
    want=$(stat -f %z "$STAGE/$src")
    line=$(sshdev "ls -l '$dst' 2>/dev/null")
    got=$(print -r -- "$line" | awk '{print $5}')
    perm=$(print -r -- "$line" | awk '{print $1}')
    if [[ -z "$got" ]];           then printf "  ABSENT    %-56s want %s\n" "$dst" "$want"; return 1
    elif [[ "$got" == "$want" ]]; then printf "  OK        %-56s %s bytes  %s\n" "$dst" "$got" "$perm"; return 0
    else                               printf "  MISMATCH  %-56s have %s, want %s\n" "$dst" "$got" "$want"; return 1
    fi
}

if (( CHECK_ONLY )); then
    print "\n[*] --check: current state on the device\n"
    for e in "${FILES[@]}"; do report "${e%%|*}" "${e##*|}"; done
    print "\n[*] staging dir left in place: $STAGE"
    exit 0
fi

# ---------------------------------------------------------------- install
print ""
for e in "${FILES[@]}"; do
    src="${e%%|*}"; dst="${e##*|}"
    printf "  %-56s " "$dst"
    if "$SSHPASS" -p alpine scp "${SCP_OPTS[@]}" "$STAGE/$src" "root@localhost:$dst" >/dev/null 2>&1; then
        print "sent"
    else
        print "FAILED"
        print -u2 "[!] stopping. Files sent before this point are already in place."
        exit 1
    fi
done

# scp does not reliably carry the executable bit here. Try chmod from whichever location
# actually has one: the ramdisk may not, and a System-volume binary may or may not run from
# the ramdisk's dyld cache. Not fatal if none work, so report instead of dying.
CHMOD=$(sshdev 'for c in /bin/chmod /usr/bin/chmod /mnt1/bin/chmod; do [ -x "$c" ] && { echo "$c"; break; }; done' 2>/dev/null)
if [[ -n "$CHMOD" ]]; then
    sshdev "$CHMOD 755 /mnt1/usr/local/bin/dropbear /mnt1/usr/local/bin/dropbearkey 2>/dev/null
            $CHMOD 600 /mnt1/private/etc/dropbear/dropbear_rsa_host_key \
                       /mnt1/private/etc/dropbear/dropbear_ecdsa_host_key \
                       /mnt1/private/etc/dropbear/dropbear_dss_host_key 2>/dev/null" >/dev/null 2>&1
    print "\n[*] permissions set using $CHMOD"
else
    print "\n[!] no chmod found on the device; check the mode column below and fix by hand"
fi
sshdev sync >/dev/null 2>&1

# ---------------------------------------------------------------- verify
print "\n[*] verifying on-device\n"
fail=0
for e in "${FILES[@]}"; do report "${e%%|*}" "${e##*|}" || fail=1; done

print ""
if (( fail )); then
    print -u2 "[!] one or more files did not land."
    exit 1
fi
print "[+] dropbear installed and verified."
print ""
print "    This only installs the binaries. Nothing in ssh.tar.gz is a LaunchDaemon, so"
print "    dropbear is started by the com.dropbear job that patch_launchd_cache.py adds"
print "    to the signed cache; fetch_payloads.sh builds it and sshrd_provision.sh"
print "    deploys it. See README.md steps 7 and 8."
print ""
print "    Staging dir left at: $STAGE"
