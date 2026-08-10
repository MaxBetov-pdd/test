#!/bin/sh
# fetch_payloads.sh - build every device payload from scratch, on the host.
#
# Run this before sshrd_provision.sh. It downloads the upstream releases,
# extracts the bundles, re-signs everything ad-hoc, and builds the local helper
# binaries from source. Output lands in payload/ and is what the provisioning
# script deploys.
#
# This exists because the payloads are build artifacts and are not committed.
# Without it a fresh clone cannot reproduce anything, and Sileo in particular
# used to be sourced from the device itself (installed by dpkg, pulled back,
# re-signed), which made provisioning depend on the device already being
# half-configured. Everything now comes from upstream.
#
#   ./fetch_payloads.sh            # everything
#   ./fetch_payloads.sh sileo      # just one
#
# Why every binary is re-signed ad-hoc:
#   Sileo ships flags=0x0 "no signature" with ZERO entitlements, because a real
#   jailbreak grants them via trust cache plus an amfid patch. As shipped it
#   dies on '/var/jb/usr/lib/libzstd.1.dylib (blocked by sandbox)'.
#   get-task-allow is never granted: AMFI kills ad-hoc binaries carrying it, so
#   Sileo's replacement entitlement set is written from scratch rather than
#   filtered from what it shipped with.
#
# TrollStore is deliberately NOT built here any more. It used to download
# opa334's TrollStore.tar, re-sign it, and rename the bundle to
# TrollStoreLite.app, which meant the device got the FULL build wearing a Lite
# name, with a helper that cannot register apps on iOS 27:
#   - registerApplicationDictionary: is a stub that always returns NO
#   - its replacement is entitlement-gated and the stock helper lacks the keys
#   - ldid was looked up on the wrong prefix
# It now installs after first boot from a deb, which also means it can be
# updated without another DFU trip, unlike anything placed on the sealed
# System volume. See COMMANDS.md, "TrollStore Lite (after first boot)".

set -e
BASE="$(cd "$(dirname "$0")" && pwd)"
cd "$BASE"

# Host-side helper binaries. Where tools/ sits is the only thing that differs
# between the repo and research copies, so it is resolved once here.
TOOLS="$BASE/../tools"

LDID="$TOOLS/ldid_macosx_arm64"
IPSW_ROOT=/tmp/ios27-rootfs           # decrypted root filesystem, mounted
OUT="$BASE/payload"
WORK="$BASE/payload/.work"

SILEO_VER=2.5.1
SILEO_DEB="org.coolstar.sileo_${SILEO_VER}_iphoneos-arm64.deb"   # arm64 == rootless
SILEO_URL="https://github.com/Sileo/Sileo/releases/download/${SILEO_VER}/${SILEO_DEB}"
SILEO_SHA=8e3c90e5a7d32f4ca207a0ac30d3cfa8   # first 32 of sha256, checked below

say()  { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
ok()   { printf '    [+] %s\n' "$1"; }
skip() { printf '    [=] %s\n' "$1"; }
die()  { printf '    [!] %s\n' "$1"; exit 1; }

[ -x "$LDID" ] || die "ldid not found at $LDID"
mkdir -p "$OUT" "$WORK"

WANT="${*:-sileo helpers cache}"
wants() { case " $WANT " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }


# ------------------------------------------------------------------- sileo
if wants sileo; then
    say "Sileo $SILEO_VER"
    deb="$WORK/$SILEO_DEB"
    [ -f "$deb" ] || curl -sL --fail -o "$deb" "$SILEO_URL" || die "download failed: $SILEO_URL"
    got=$(shasum -a 256 "$deb" | cut -c1-32)
    [ "$got" = "$SILEO_SHA" ] || die "sha256 mismatch: got $got expected $SILEO_SHA"
    ok "downloaded and hash-verified"

    rm -rf "$WORK/sileo" && mkdir -p "$WORK/sileo"
    ( cd "$WORK/sileo" && ar x "$deb" && xz -dc data.tar.xz | tar xf - )
    APP="$WORK/sileo/var/jb/Applications/Sileo.app"
    [ -d "$APP" ] || die "Sileo.app not found in the deb"
    # Verify setuid in the ARCHIVE, not the extracted copy: macOS tar drops
    # setuid bits when extracting as a non-root user, so the extracted file
    # never has it and checking there would always fail. It is restored with an
    # explicit chmod below, and the device-side tar (running as root) preserves
    # what we set.
    xz -dc "$WORK/sileo/data.tar.xz" | tar tvf - 2>/dev/null \
        | grep -q '^-rws.*giveMeRoot$' \
        || die "giveMeRoot is not setuid in the deb; refusing"
    ok "extracted, giveMeRoot setuid confirmed in the archive"

    # Sileo needs sandbox exemption to load dylibs out of /var/jb. Its own
    # 34-entitlement set cannot be granted wholesale: AMFI SIGKILLs an ad-hoc
    # binary claiming all of them (one of the 30 we do not need is restricted).
    cat > "$WORK/sileo.ent" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
	<key>platform-application</key><true/>
	<key>com.apple.private.security.no-sandbox</key><true/>
	<key>com.apple.private.security.no-container</key><true/>
	<key>com.apple.private.security.container-required</key><false/>
	<key>com.apple.private.skip-library-validation</key><true/>
	<key>com.apple.private.security.storage-exempt.heritable</key><true/>
	<key>com.apple.private.persona-mgmt</key><true/>
	<key>com.apple.private.spawn-subsystem-root</key><true/>
</dict></plist>
EOF
    "$LDID" -Iorg.coolstar.SileoStore -S"$WORK/sileo.ent" -Cadhoc "$APP/Sileo"
    "$LDID" -IgiveMeRoot -S"$WORK/sileo.ent" -Cadhoc "$APP/giveMeRoot"
    chmod 4755 "$APP/giveMeRoot"
    [ -u "$APP/giveMeRoot" ] || die "giveMeRoot lost setuid after signing"

    # CodeResources seals giveMeRoot (the main binary is excluded, its signature
    # is embedded). Re-signing giveMeRoot changes its bytes, which leaves that
    # seal entry pointing at a file that no longer exists. Rewrite just that
    # entry rather than leaving the bundle internally inconsistent.
    python3 - "$APP" <<'PY'
import plistlib, hashlib, sys, os
app = sys.argv[1]
cr = os.path.join(app, "_CodeSignature", "CodeResources")
if not os.path.isfile(cr):
    print("        no CodeResources, nothing to reseal"); raise SystemExit
d = plistlib.load(open(cr, "rb"))
fixed = 0
for section in ("files", "files2"):
    files = d.get(section)
    if not isinstance(files, dict):
        continue
    for name, ent in files.items():
        target = os.path.join(app, name)
        if os.path.basename(name) != "giveMeRoot" or not os.path.isfile(target):
            continue
        blob = open(target, "rb").read()
        if isinstance(ent, dict):
            if "hash2" in ent: ent["hash2"] = hashlib.sha256(blob).digest()
            if "hash"  in ent: ent["hash"]  = hashlib.sha1(blob).digest()
        else:
            files[name] = hashlib.sha1(blob).digest()
        fixed += 1
plistlib.dump(d, open(cr, "wb"))
print(f"        resealed {fixed} CodeResources entr{'y' if fixed==1 else 'ies'} for giveMeRoot")
PY

    rm -rf "$OUT/Sileo.app" && cp -R "$APP" "$OUT/Sileo.app"
    chmod 4755 "$OUT/Sileo.app/giveMeRoot"
    ok "payload/Sileo.app ready ($(codesign -dv "$OUT/Sileo.app/Sileo" 2>&1 | grep -o 'flags=0x[0-9a-f]*([a-z]*)'))"
fi

# ------------------------------------------------------------------- cache
# Build the launchd service cache from the IPSW, not from the device. It used
# to be pulled off a live phone, which meant you needed an already-provisioned
# device to provision a device.
if wants cache; then
    say "launchd service cache"
    STOCK="$IPSW_ROOT/System/Library/xpc/launchd.plist"
    if [ ! -f "$STOCK" ]; then
        skip "IPSW not mounted at $IPSW_ROOT; cannot build the cache"
    else
        mkdir -p boot/work
        cp "$STOCK" boot/work/launchd.plist
        ok "stock cache from the IPSW ($(wc -c < boot/work/launchd.plist | tr -d ' ') bytes)"
        ./patch_launchd_cache.py boot/work/launchd.plist --apply >/dev/null \
            || die "failed to add com.dropbear"
        ok "com.dropbear added"
        ./add_jbboot.py boot/work/launchd.plist --apply >/dev/null \
            || die "failed to add com.jbboot"
        ok "com.jbboot added"
        n=$(python3 -c "import plistlib;print(len(plistlib.load(open('boot/work/launchd.plist','rb'))['LaunchDaemons']))")
        ok "boot/work/launchd.plist ready, $n daemons"
        echo "        the detached .sig on the device is left untouched; the loader"
        echo "        accepts a modified cache because of launchd_unsecure_cache=1"
    fi
fi

# ------------------------------------------------------------------ helpers
if wants helpers; then
    say "local helper binaries"
    for d in photodiag appreg spawnprobe; do
        [ -d "$d" ] || continue
        if [ -x "$d/build.sh" ]; then
            # if-then-else, not A && B || C: with the latter, a failing ok()
            # would run the skip() branch even on a successful build
            if ( cd "$d" && ./build.sh >/dev/null 2>&1 ); then
                ok "$d built"
            else
                skip "$d build failed"
            fi
        else
            skip "$d has no build.sh"
        fi
    done
fi

say "summary"
for p in "$OUT/Sileo.app/Sileo" "$OUT/Sileo.app/giveMeRoot" \
         photodiag/photodiag spawnprobe/personaalloc appreg/appreg; do
    if [ -f "$p" ]; then
        # "$BASE" quoted separately inside ${..}: unquoted it is treated as a
        # glob pattern, so a path containing [ or * would strip the wrong prefix
        printf '    %-46s %8s bytes  %s\n' "${p#"$BASE"/}" "$(wc -c < "$p" | tr -d ' ')" \
            "$(codesign -dv "$p" 2>&1 | grep -o 'flags=0x[0-9a-f]*([a-z]*)' || echo '')"
    else
        printf '    %-46s MISSING\n' "${p#"$BASE"/}"
    fi
done
echo
echo "    next: boot the device into SSHRD, then ./sshrd_provision.sh"
