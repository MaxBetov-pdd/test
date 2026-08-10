# jbboot.sh - per-boot setup that cannot persist on disk.
#
# Run by launchd at every normal boot via the com.jbboot job in the signed
# service cache, as:  /bin/sh /var/jb/bin/jbboot.sh
#
# NO SHEBANG: the kernel refuses to exec interpreters here ("bad interpreter:
# Operation not permitted"), so the interpreter is named in the job's
# ProgramArguments instead. A shebang line here would be misleading.
#
# ---------------------------------------------------------------------------
# THIS SCRIPT MUST USE ONLY SHELL BUILTINS AND /usr/local/bin/personaalloc.
#
# The System volume is nearly empty. Verified on 24A5390f:
#     /bin            cat df ls ps sh
#     /usr/bin        29 entries, all diagnostics (sysdiagnose, taskinfo, ...)
#     /usr/local/bin  dropbear dropbearkey
# There is no date, id, grep, mkdir, chmod or chown anywhere on it. Everything
# normally used over SSH comes from the bootstrap at /var/jb via PATH.
#
# And launchd cannot exec bootstrap binaries at all: pointing the job at
# /var/jb/bin/sh failed with "posix_spawn(/var/jb/bin/sh), error 0x1 -
# Operation not permitted". So anything this script needs must either be a
# builtin or live on the System volume. personaalloc is therefore installed to
# /usr/local/bin, the same directory as dropbear, which launchd demonstrably
# execs without trouble.
#
# An earlier version called date, id, grep, mkdir, chown and chmod. It would
# have failed on the first line.
# ---------------------------------------------------------------------------
#
# Log: /var/mobile/jbboot.log

LOG=/var/mobile/jbboot.log
PERSONAALLOC=/usr/local/bin/personaalloc

# echo and redirection are builtins; no external command is involved.
# There is no date on this volume, so entries are unstamped. launchd records
# the job's start time in /var/log/com.apple.xpc.launchd/launchd.log if the
# ordering ever matters.
echo "=== jbboot start ===" >> "$LOG"

# --- persona 99 -------------------------------------------------------------
# Sileo's spawnAsRoot() does posix_spawn with persona 99 + uid/gid 0. The
# persona table is empty at every boot (0 personas across ids 0..120) because a
# normal first boot never completed here, so that spawn fails ESRCH and Sileo
# silently does nothing. Personas are in-kernel state and do not survive a
# reboot, which is the entire reason this job exists.
#
# NOTE: this alone does not make Sileo installs work. With persona 99 present
# the error becomes EPERM, because a non-root process still cannot adopt a
# persona on this kernel. That needs a spawn_validate_persona patch. Creating
# the persona is a prerequisite for that fix, not the fix itself.
if [ -x "$PERSONAALLOC" ]; then
    out=$("$PERSONAALLOC" 2>&1)
    # case, not grep: grep does not exist on this volume
    case "$out" in
        *"YES: id=99"*)
            echo "persona 99 created" >> "$LOG"
            ;;
        *)
            echo "persona 99 FAILED, output follows:" >> "$LOG"
            echo "$out" >> "$LOG"
            ;;
    esac
else
    echo "personaalloc not found at $PERSONAALLOC" >> "$LOG"
fi

# Sileo's sileolists directory is deliberately NOT recreated here. It lives on
# the Data volume and persists across reboots, and creating it would need mkdir,
# chown and chmod, none of which exist on the System volume.

echo "=== jbboot done ===" >> "$LOG"
exit 0
