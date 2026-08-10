#!/bin/zsh
#
# Normal boot chain. Original preserved as boot.py.orig.
#
# Changes from upstream:
#
#  1. Restored the two sleeps that boot_rd.sh has and this script had commented out
#     (before PMP, and before DeviceTree). boot_rd.sh boots reliably; this one booted
#     six times and then stopped with an unchanged build, which is what a timing-
#     sensitive USB handshake looks like.
#
#  2. Every step checks its exit status and aborts on failure. Upstream ignores all 16
#     return codes, so a component rejected by iBoot is indistinguishable from a
#     successful boot that we simply cannot see.
#
#  3. Each step prints its name before running, so a failure names itself instead of
#     leaving a wall of identical progress bars.
#
#  4. bgcolor moved BEFORE setpicture. bgcolor fills the framebuffer, so running it
#     afterwards (as boot_rd.sh does) overwrites the logo. Note: on this device nothing
#     draws at all, so this is correctness rather than something we can see.

set -u

fail() { printf '\n[!] %s\n' "$1"; exit 1; }

# send <label> <file> <iboot-command>
send() {
    printf '  %-24s ' "$1"
    irecovery -f "$2" >/dev/null 2>&1 || fail "$1: upload failed ($2)"
    irecovery -c "$3" >/dev/null 2>&1 || fail "$1: command failed ($3)"
    printf 'ok\n'
}

# cmd <label> <iboot-command>
cmd() {
    printf '  %-24s ' "$1"
    irecovery -c "$2" >/dev/null 2>&1 || fail "$1: command failed ($2)"
    printf 'ok\n'
}

echo "[*] stage 1: iBSS via pwned DFU"
printf '  %-24s ' "iBSS (usbliter8)"
../tools/usbliter8ctl boot ./Ramdisk/iBSS.raw >/dev/null 2>&1
# usbliter8ctl always errors on its trailing DFU_ABORT: CUSTOM_BOOT already made the
# device jump to iBSS, so the handle is gone. Not a failure, so the status is ignored
# here. If iBSS had not booted, the iBEC upload below would fail and be caught.
printf 'sent\n'
sleep 3

echo "[*] stage 2: iBEC"
printf '  %-24s ' "iBEC"
irecovery -f Ramdisk/iBEC.img4 >/dev/null 2>&1 || fail "iBEC upload failed (is the device in pwned DFU?)"
irecovery -c go >/dev/null 2>&1 || fail "iBEC 'go' failed"
printf 'ok\n'
sleep 2

echo "[*] stage 3: display"
cmd  "bgcolor"        "bgcolor 0 191 255"
send "RestoreLogo"    Ramdisk/RestoreLogo.img4 "setpicture 0x1"

echo "[*] stage 4: firmware components"
send "ANE"            Ramdisk/ANE.img4   firmware
send "AOP"            Ramdisk/AOP.img4   firmware
send "AVE"            Ramdisk/AVE.img4   firmware

sleep 1
send "SPTM"           Ramdisk/SPTM.img4  firmware

sleep 3
send "TXM"            Ramdisk/TXM.img4   firmware

send "GFX"            Ramdisk/GFX.img4   firmware
send "ISP"            Ramdisk/ISP.img4   firmware

# Restored sleep. Upstream commented this out; boot_rd.sh keeps it.
sleep 1
# PMP resolves: launchd "rebooting due to critical process crashes: SpringBoard"
send "PMP"            Ramdisk/PMP.img4   firmware

send "RestoreTrustCache" Ramdisk/RestoreTrustCache.img4 firmware
# SIO is the touch driver
send "SIO"            Ramdisk/SIO.img4   firmware
send "WCH"            Ramdisk/WCH.img4   firmware

echo "[*] stage 5: devicetree, sep, kernel"
# Restored sleep, present in boot_rd.sh before the DeviceTree upload.
sleep 2
send "DeviceTree"     Ramdisk/DeviceTree.img4  devicetree
send "SEP"            Ramdisk/SEP.img4         rsepfirmware

printf '  %-24s ' "Kernelcache"
irecovery -f Ramdisk/Kernelcache.img4 >/dev/null 2>&1 || fail "Kernelcache upload failed"
printf 'uploaded\n'

echo "[*] stage 6: bootx"
# bootx hands control to the kernel, so the USB handle disappears and irecovery may
# report an error even on success. Status deliberately not checked.
irecovery -c bootx >/dev/null 2>&1
echo "  booted (device should now be off USB while the kernel starts)"
sleep 1

cat <<'EOF'

[*] The device is now booting iOS. There is no way to observe this live:

      - the display is black from iBEC onward (open bug, panel itself is fine)
      - stock iOS has no SSH; dropbear cannot be added, launchd takes its whole
        service list from the signed cache at /System/Library/xpc/launchd.plist
        and never reads /System/Library/LaunchDaemons
      - USB stays absent until lockdownd is up, which is late and unreliable

    So a blank screen and an absent device mean NOTHING. Every boot judged a
    failure by those signals so far had in fact succeeded.

    Wait ~5 minutes, then go back to pwned DFU and read what happened:

        ./get_rd.py && ./boot_rd.sh
        cd .. && ./wait_for_ssh.sh && ./sshrd_collect.sh

    usermanagerd logs one line per iOS boot, so a fresh timestamp there is the
    only trustworthy proof the boot worked.
EOF
