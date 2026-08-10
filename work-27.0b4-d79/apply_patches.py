#!/usr/bin/env python3
"""
Apply firmware patches with a hard assertion on the original bytes.

The upstream usbliter8-fun scripts write patches blind: `fp.seek(off); fp.write(word)`.
If an offset is wrong, or right for a different build, the write still succeeds and you
find out on the device, from a boot that hangs with no useful output.

Every patch here carries the word we believe is currently at that offset. Nothing is
written unless the file actually contains it. A wrong offset fails at the desk instead
of on the phone.

    ./apply_patches.py ibss-restore iBSS.raw               dry run, verifies only
    ./apply_patches.py ibss-restore iBSS.raw -o out.raw    verify then write
    ./apply_patches.py --list

Offsets selected by TABLES below are for d79 / 24A5390f only; see README.md in this
directory. The original n104 definitions are retained as semantic templates for the
relocated d79 kernel tables.
iBSS and iBEC extract to byte-identical payloads on this build, so the same table serves
both. Verified d79 sha256: 2749ee482b68342df82ec48f37148c29c70fc9d347d0b20005a6ff6c2d5b8f71.
"""

import argparse
import hashlib
import struct
import sys
from pathlib import Path

NOP = 0xD503201F
MOV_X0_0 = 0xD2800000


class Word:
    """A 4-byte instruction patch guarded by the word expected to be there."""

    kind = "word"

    def __init__(self, off, orig, new, comment):
        self.off, self.orig, self.new, self.comment = off, orig, new, comment

    def check(self, data):
        if self.off + 4 > len(data):
            return False, f"offset past end of file ({len(data)} bytes)"
        got = struct.unpack_from("<I", data, self.off)[0]
        if got == self.orig:
            return True, f"{got:#010x} -> {self.new:#010x}"
        if got == self.new:
            return None, f"already patched ({got:#010x})"
        return False, f"expected {self.orig:#010x}, found {got:#010x}"

    def apply(self, buf):
        struct.pack_into("<I", buf, self.off, self.new)


class Blob:
    """A string/byte patch into space asserted to be zeroed first."""

    kind = "blob"

    def __init__(self, off, room, payload, comment):
        self.off, self.room, self.comment = off, room, comment
        self.payload = payload if isinstance(payload, bytes) else payload.encode() + b"\x00"

    def check(self, data):
        if len(self.payload) > self.room:
            return False, f"payload {len(self.payload)} > room {self.room}"
        if self.off + self.room > len(data):
            return False, f"offset past end of file ({len(data)} bytes)"
        region = data[self.off:self.off + self.room]
        if not any(region):
            return True, f"{len(self.payload)} bytes into {self.room:#x} of zeroes"
        if region.startswith(self.payload):
            return None, "already written"
        return False, f"scratch at {self.off:#x} is not zeroed (found {region[:16].hex()})"

    def apply(self, buf):
        buf[self.off:self.off + len(self.payload)] = self.payload


class Text:
    """An in-place string replacement guarded by the exact bytes expected to be there.

    Unlike Blob, the destination is not empty: it holds a known string of the same length.
    Used for the kernel version strings, where "/RELEASE_ARM64_T8030" becomes
    "/PATCHED_ARM64_T8030" so a patched kernel is identifiable at runtime.
    """

    kind = "text"

    def __init__(self, off, orig, new, comment):
        self.off, self.comment = off, comment
        self.orig = orig if isinstance(orig, bytes) else orig.encode()
        self.payload = new if isinstance(new, bytes) else new.encode()
        if len(self.orig) != len(self.payload):
            raise ValueError(f"{off:#x}: replacement must match original length")

    def check(self, data):
        n = len(self.orig)
        if self.off + n > len(data):
            return False, f"offset past end of file ({len(data)} bytes)"
        got = data[self.off:self.off + n]
        if got == self.orig:
            return True, f"{self.orig.decode()!r} -> {self.payload.decode()!r}"
        if got == self.payload:
            return None, "already patched"
        return False, f"expected {self.orig!r}, found {got!r}"

    def apply(self, buf):
        buf[self.off:self.off + len(self.payload)] = self.payload


# --- d79 / 24A5390f iBSS+iBEC --------------------------------------------------------
# Site 1: image4_validate_property_callback. Kill the failure branch, force return 0.
VALIDATE = [
    Word(0x22F18, 0x54000701, NOP,
         "failure branch; never take it"),
    Word(0x22F1C, 0xAA1403E0, MOV_X0_0,
         "was mov x0, x20 (status); force success"),
]

# Site 2: repoint the boot-args snprintf format string at our own literal.
# Slot 0xcffb0 is inside a verified zero-filled section tail (0xcffa4..0xd0000), with
# enough room for every string used below while staying clear of the next page.
BOOTARGS_SITE = 0x2A218
BOOTARGS_SLOT = 0xCFFB0
BOOTARGS_ROOM = 0x50


def bootargs(text):
    return [
        Word(BOOTARGS_SITE, 0xB0000862, 0xB0000522,
             f"repoint adrp x2 to {BOOTARGS_SLOT & ~0xFFF:#x}"),
        Word(BOOTARGS_SITE + 4, 0x913E0C42, 0x913EC042,
             f"repoint add x2 to #{BOOTARGS_SLOT & 0xFFF:#x}"),
        Blob(BOOTARGS_SLOT, BOOTARGS_ROOM, text, f"boot-args: {text!r}"),
    ]


# --- n104 / 24A5390f restore ramdisk (094-13753-162.dmg) -----------------------------
# restored_external: force the FDR step to report success.
# Same shape as b2's known-good 0x7e53c: the `mov x0, <ret>` immediately before the
# epilogue becomes `mov x0, #0`. Anchored on the single xref to "RestoredFDRRecover".
RESTORED_EXTERNAL = [
    Word(0x7E558, 0xAA1A03E0, MOV_X0_0,
         "was mov x0, x26 before ldp x29,x30,[sp,0x90]; force FDR success"),
]

# asr: neutralise the image signature check.
#
# NOTE: b3 publishes 0x1f650 for this. Do NOT reuse that number here. In the b4 ramdisk
# 0x1f650 is the `bl memcmp` itself, and nopping it leaves w0 holding x29-0x98, a stack
# address that is never zero, so the following `cbnz w0` would branch to failure every
# time. b2's known-good patch nops the CBNZ, not the BL. Here that is 0x1f654.
ASR = [
    Word(0x1F654, 0x35003120, NOP,
         "was cbnz w0, 0x1fc78 after bl memcmp; digest mismatch must not branch"),
]

# --- n104 / 24A5390f TXM (txm.iphoneos.release.im4p) ---------------------------------
# VA = file_offset + 0xFFFFFFF017004000.
#
# queryModule0/1/2: each does memcmp(a, b, 0x14) on a truncated CDHash and branches on
# the result. Replacing the `bl memcmp` with `mov x0, #0` makes every hash "match", so
# binaries absent from the trustcache run. Logged as: CodeSignature: selector: 24|0xA8|0x30|1
#
# There are four `mov w2,#0x14 ; bl memcmp` sites in the image. The three real ones are
# followed by `cbz w0`; the fourth is followed by `cmp w0, 0` and is NOT patched upstream.
TXM_QUERYMODULE = [
    Word(0x39E28, 0x97FFFA65, MOV_X0_0, "queryModule0: bl memcmp -> mov x0,#0"),
    Word(0x39F90, 0x97FFFA0B, MOV_X0_0, "queryModule1: bl memcmp -> mov x0,#0"),
    Word(0x3A124, 0x97FFF9A6, MOV_X0_0, "queryModule2: bl memcmp -> mov x0,#0"),
]

# validateConstraintsSignatureType: two branches that skip forward to the block which can
# reach `mov w0,0x30a1 ; movk w0,1,lsl 16` == 0x000130A1, the status logged as
# "selector: 24 | 0xA1 | 0x30 | 1". Nopping both forces the fall-through path, which sets
# w0 = 0xa1 and branches directly into the epilogue, past the error assignment.
# The restricted task-port/debugger entitlement validator, also under selector 24
# (RegisterCodeSignature), is a separate function at 0x3F598. It walks a six-entry table
# at VA 0xFFFFFFF01701CCD0 and rejects the signature if the binary claims ANY of:
#
#     [0] get-task-allow                 [3] com.apple.system-task-ports
#     [1] com.apple.private.cs.debugger  [4] com.apple.system-task-ports.control
#     [2] task_for_pid-allow             [5] com.apple.system-task-ports.token.control
#
# Loop body is 0x3F60C..0x3F634. w19 preloads 0x000130A2 and gains 0x10000 per iteration,
# so a rejection returns 0x00NN30A2, logged as "selector: 24 | 0xA2 | 0x30 | N".
# `tst w0,#0xff00 ; b.eq` takes the error exit when the entitlement IS present. NOPing the
# branch lets all six iterations run; the loop falls out at 0x3F638 to 0x3F5C4, which sets
# w19 = 0xa2 and returns success. x21 still steps 8 to 0x30, so the count cannot run away.
#
# This is the gate that survived the developer_mode_state() kernelcache patch: binaries
# carrying these keys were SIGKILLed at exec with no log at all, because the rejection is
# in TXM rather than AMFI. CONFIRMED on device: all six now exec (RC 0, previously 137).
#
# Trusted because two methods agreed. On-device bisection marked get-task-allow,
# task_for_pid-allow, com.apple.system-task-ports and .control fatal one at a time, and
# this table holds exactly those four plus two never tested.
TXM_CONSTRAINTS = [
    Word(0x3F624, 0x54FFFD20, NOP,
         "was b.eq (reject six restricted task-port/debugger entitlements)"),
    Word(0x3F690, 0x540000A3, NOP, "was b.lo (skip toward 0x000130A1 error path)"),
    Word(0x3F698, 0xB4000069, NOP, "was cbz x9 (skip toward 0x000130A1 error path)"),
]

# Developer mode: make TXM publish it as TRUE. CONFIRMED on device.
#
# TXM computes developer-mode state and publishes a byte in the structure at VA
# 0xFFFFFFF017084DB0 (x19 is loaded with it at 0x2BA54, the same structure
# TXM_SECURECHANNEL touches at 0x2BCD8). XNU receives that address over selector 2 and
# caches the pointer at 0xFFFFFFF007C14060; 16 kernel sites then load it and read the byte.
#
# WHY THIS AND NOT ONLY THE KERNEL ACCESSOR. KC_AMFI forces developer_mode_state() -> true
# and that fixed AMFI's gates. But only ONE of the 16 readers is that accessor; the other
# 15 inline the load. task_conversion_eval is one of them:
#
#     0x3223770  adrp x8, 0xfffffff007c14000
#     0x3223774  ldr  x8, [x8, #0x60]
#     0x322377C  ldrb w8, [x8]
#     0x3223780  tbnz w8, #0, <permissive>   ; reads the real byte, still 0
#     0x3223784  cmp  x0, x1
#     0x3223788  b.eq <permissive>           ; caller == victim, why self worked
#
# which is exactly why task_for_pid returned MACH_PORT_DEAD for every process except our
# own while AMFI's own gate allowed. Fixing the source byte repairs all 16 at once.
# Measured after boot: launchd, amfid and a foreign ad-hoc helper all return live control
# ports and task_info succeeds on each.
#
# WHY THE tbz AND NOT THE FINAL strb. Patching the final store would publish true while
# TXM's internal propagation had already received false. The fallthrough here is already
# `mov w20, #1 ; b <publish>`, so NOPing the branch sends true through TXM's existing
# secure-channel propagation and on to the XNU-visible byte.
#
# Normal boot only. The restore chain has no use for developer mode, and this is a real
# widening of what the device permits, so it is kept out of txm-restore deliberately.
TXM_DEVMODE = [
    Word(0x2BA58, 0x36000069, NOP,
         "was tbz w9,#0 (skip 'mov w20,#1' -> publish dev mode true)"),
]

# allowedBeforeSecureChannelOperational -> always true. Patched right after the `bti c`
# landing pad at 0x2bcd0, same as upstream does.
#
# NOTE: b2 AND b3 both publish 0x2bcb4 for this, which is b2's offset copied forward.
# In the b4 TXM, 0x2bcb4 is in the MIDDLE of an unrelated function's epilogue
# (ldp x29,x30 / ldp x20,x19 / add sp / retab). Patching there would corrupt it.
# The real entry in b4 is 0x2bcd4.
TXM_SECURECHANNEL = [
    Word(0x2BCD4, 0xB00002A9, 0xD2800020, "was adrp x9, 0x80000; now mov x0, #1"),
    Word(0x2BCD8, 0x9136C129, 0xD65F03C0, "was add x9, x9, 0xdb0; now ret"),
]

# --- n104 / 24A5390f kernelcache (kernelcache.release.iphone12b) ---------------------
# MH_FILESET, stripped (only auto-generated kernel.init.N symbols survive).
# VA = file_offset + 0xFFFFFFF007004000.
#
# n104 uses kernelcache.release.iphone12b; d421 used kernelcache.release.iphone12, so this
# is a different KC variant as well as a different build. Every offset was re-derived.
RET = 0xD65F03C0

# Cosmetic: mark the kernel as patched so uname distinguishes it from stock.
# Same length in place, so no layout change.
KC_KERNELNAME = [
    Text(0x3F2C4, "/RELEASE_ARM64_T8030", "/PATCHED_ARM64_T8030",
         "kernel version string 1 of 2"),
    Text(0x3F330, "/RELEASE_ARM64_T8030", "/PATCHED_ARM64_T8030",
         "kernel version string 2 of 2"),
]

# SSV and data-protection panics. Each is a conditional branch into the panic block;
# nopping it means the block is never entered. Located by finding the panic format string,
# then the conditional branch whose target lands closest before the string's only xref.
# The original words are identical to b2's at all four sites.
KC_PANICS = [
    Word(0x3052800, 0x37280628, NOP,
         "_apfs_vfsop_mount: was tbnz w8,5 -> 'Failed to find the root snapshot'"),
    Word(0x2FC0CEC, 0x37700160, NOP,
         "_authapfs_seal_is_broken: was tbnz w0,0xe -> 'root volume seal is broken'"),
    Word(0x36D5B48, 0x35001300, NOP,
         "_bsd_init: was cbnz w0 -> 'rootvp not authenticated after mounting'"),
    Word(0x3053C10, 0x370000A8, NOP,
         "was tbnz w8,0 -> 'unencrypted data volume is not allowed'"),
]

# AMFI and codesigning. No strings to anchor on, so each was located with a masked
# instruction signature taken from b2's known-good offset (see sigmatch.py).
KC_AMFI = [
    # AMFIIsCDHashInTrustCache -> report every hash as present, and write 1 through x3.
    # Replaces the prologue, so `bti c` keeps the branch-target landing pad valid.
    Word(0x1F2FFC8, 0xD503237F, 0xD503245F, "AMFIIsCDHashInTrustCache: pacibsp -> bti c"),
    Word(0x1F2FFCC, 0xD100C3FF, 0xD2800020, "  mov x0, #1"),
    Word(0x1F2FFD0, 0xA9014FF4, 0xB4000043, "  cbz x3, +8"),
    Word(0x1F2FFD4, 0xA9027BFD, 0xF9000060, "  str x0, [x3]"),
    Word(0x1F2FFD8, 0x910083FD, RET, "  ret"),
    # proc_check_launch_constraints -> 0 (allow)
    Word(0x1F34BF0, 0xD503237F, 0x52800000, "proc_check_launch_constraints: mov w0, #0"),
    Word(0x1F34BF4, 0xD10603FF, RET, "  ret"),
    # PE_i_can_has_debugger -> 1
    Word(0x3A1C748, 0x90FE9248, 0xD2800020, "PE_i_can_has_debugger: mov x0, #1"),
    Word(0x3A1C74C, 0xB40000E0, RET, "  ret"),
    # developer_mode_state -> true. Distinct from PE_i_can_has_debugger above.
    #
    # AMFI calls this accessor while walking restricted entitlements; when it returns
    # false, get-task-allow, task_for_pid-allow and the system-task-ports family
    # invalidate the signature before the process reaches main. AMFI's task_for_pid()
    # authorisation at 0x1F3C104 consults it independently at 0x1F3C21C, so patching only
    # the entitlement-walk branch would allow exec and still deny the task-port operation.
    # Both of those were confirmed on device.
    #
    # NOTE: this fixes only ONE of the 16 kernel sites that read developer-mode state. The
    # other 15 inline the load (`adrp x8, 0xfffffff007c14000 ; ldr x8,[x8,#0x60] ; ldrb`)
    # and never see it. task_conversion_eval at 0x3223770 is one of them, which is why
    # task ports stayed dead until TXM_DEVMODE made TXM publish the real byte as true.
    # Kept alongside TXM_DEVMODE rather than removed: harmless, and it is the reason the
    # AMFI-side gates pass.
    Word(0x376351C, 0xB0FEA568, 0x52800020, "developer_mode_state: mov w0, #1"),
    Word(0x3763520, 0xF9403108, RET, "  ret"),
    # postValidation: cmp w0,2 -> cmp w0,w0, so the following b.ne is never taken.
    Word(0x1F3C750, 0x7100081F, 0x6B00001F, "postValidation: cmp w0,2 -> cmp w0,w0"),
    # _check_dyld_policy_internal: two calls replaced by 'return 1'.
    Word(0x1F3CCB0, 0x94006170, 0x52800020, "check_dyld_policy_internal: bl -> mov w0,#1"),
    Word(0x1F3CCBC, 0x97FFDF2B, 0x52800020, "check_dyld_policy_internal: bl -> mov w0,#1"),
]

# spawn_validate_persona: let an entitled non-root caller request persona UID/GID 0.
#
# Source-semantic match against XNU kern_exec.c, then confirmed in this b4 kernelcache:
#   - checks com.apple.private.persona-mgmt, returning EPERM if absent
#   - validates group count, returning EINVAL
#   - persona_lookup(pspi_id), returning ESRCH if absent
#   - rejects a non-root caller when pspi_uid == 0 OR pspi_gid == 0
#
# The two words below are exactly those final sibling rejects. Both branch to the same
# `mov w20,#1` result; NOPing them falls through to `mov w20,#0`. The entitlement,
# group-count and persona-existence checks remain unchanged.
#
# Boundary check: entry VA 0xfffffff00a714d40 (file 0x3710d40) is PACIBSP and IDA marks
# it as a separate function called from the posix_spawn persona-info copyin path. Unlike
# the usual boundary heuristic, the preceding word is not RET/RETAB: it is the final BL
# to the non-returning panic/assert routine of the previous function. Treating that BL as
# a normal return would fall through into a second PACIBSP prologue with the old frame,
# which is impossible; this is a real function boundary, not a mid-function false match.
KC_PERSONA = [
    Word(0x3710DEC, 0x340000E8, NOP,
         "spawn_validate_persona: was cbz pspi_uid, EPERM; allow UID 0"),
    Word(0x3710DF4, 0x340000A8, NOP,
         "spawn_validate_persona: was cbz pspi_gid, EPERM; allow GID 0"),
]

KC = ("d79 kernelcache payload", 67436544,
      "fd3cc0ff32bfaac16bb9a7b9cbe832ed437de0e2e7aa66f9e195e4f08abe40f2")

# --- n104 / 24A5390f kernelcache, normal-boot extras ---------------------------------
# Only get_boot.py applies these; restore and ramdisk boot do not need them.
#
# All located by masked signature from b2's known-good offsets, except
# _powerChangeNotificationHandler, which needed a string anchor: b4 saves one extra
# register pair (stp x22,x21 added, sub sp 0x150 vs 0x140) so no exact signature matched.
# It was found via the single xref to "AppleSEPManager: Received Paging off notification".
#
# The 26 AppleCredentialManager entries were cross-checked three ways: every b4 target is a
# real function entry (25 pacibsp + 1 bti c), and the function ORDER is monotonic in both
# builds, which a bad match would break.
MOV_W0_0 = 0x52800000

# Split into two groups so a diagnostic build can keep the anti-hang fixes while letting
# failures panic. Everything in KC_SEP_SILENCE exists purely to stop a panic; with them
# applied, any unhandled SEP problem becomes a silent hang that writes no log at all,
# which is exactly what made the first failed boots undiagnosable.
# externalMethod: guarded selector-7 shim, replacing the unconditional `mov x0,#0 ; ret`.
#
# WHY. The old stub returned KERN_SUCCESS without running any selector handler, so every
# AKS output buffer was left untouched. MobileKeyBag zeroes its out-param before calling
# (`str wzr, [sp,#0x1c]` in _MKBDeviceUnlockedSinceBoot), so it read bit 2 of an unwritten
# zero and reported "never unlocked since boot" forever. That is what kills the photo
# library and PosterBoard. Reverting the stub does not help: upstream's b2 comment records
# selector 7 returning e00002d8 (kIOReturnNotReady) before any stub existed, because SEP
# does not boot. Both paths fail `cmp w0, 1`, so the state has to be SYNTHESISED.
#
# ABI, verified three ways rather than assumed:
#   1. IOExternalMethodArguments field order from the Kernel SDK header, laid out for LP64:
#        +0x20 scalarInput   +0x28 scalarInputCount
#        +0x48 scalarOutput  +0x50 scalarOutputCount
#   2. The native selector-7 path in this same kernelcache checks exactly those fields:
#        0xfffffff0091377e8  ldr w8, [x19,#0x28] ; cmp w8,#1 ; b.ne <err>
#        0xfffffff0091377f4  ldr w8, [x19,#0x50] ; cmp w8,#1 ; b.ne <err>
#   3. The native store is a 64-bit write of a zero-extended 32-bit state:
#        0xfffffff00913c520  ldr w8,[sp,#0xd0] ; ldr x9,[x19,#0x48] ; str x8,[x9]
#
# VALUE. 0x6 = noPin | beenUnlocked, with `locked` clear. The AppleKeyStore constants are:
#     locked = 0x1, noPin = 0x2, beenUnlocked = 0x4
# Bit 2 was confirmed here directly: both _MKBDeviceUnlockedSinceBoot and
# _MKBUserUnlockedSinceBoot end in `ubfx w0, w8, #2, #1`. Bits 0 and 1 were confirmed
# against the AppleKeyStore constants during review. 0x6 is also the self-consistent state
# for this device, which genuinely has no passcode after the erase restore: no PIN, not
# locked, has been unlocked.
#
# SCOPE. This does not make the broad AKS stub coherent. _MKBGetDeviceLockState goes through
# __get_device_lock_state with a larger struct and reads [sp,#0x4], a different path that
# this shim does not touch. Deliberately minimal: every selector except 7 keeps the exact
# fake-success behaviour it has today.
#
# 0x213ac00 (`pacibsp`) is deliberately LEFT ALONE. It is the BTI landing pad for an
# indirect virtual call and the PAC pairing for the `retab`s below, so keeping it is
# strictly safer than the old patch, which overwrote it and returned with a bare `ret`.
# Our code touches neither SP nor LR, so the pacibsp/retab pairing stays balanced.
#
# The 20 words below were produced by clang (`-arch arm64e`), not hand-encoded, then
# disassembled back with capstone to confirm the branch targets land on Lok/Lbad.
# Source kept at aks_shim/shim.s.
KC_AKS = [
    # These prevent hangs rather than silencing panics, so a diagnostic build keeps them.
    Word(0x212fb30, 0xd73f0910, NOP, 'AKSUserClient::start (fixes stuck keybagd init)'),

    # externalMethod+0x04 .. +0x50. Entry pacibsp at 0x213ac00 intentionally untouched.
    Word(0x213ac04, 0xd10683ff, 0x71001c3f, 'externalMethod: cmp w1, #7   (selector)'),
    Word(0x213ac08, 0xa9146ffc, 0x540001c1, '  b.ne Lok                   (other selectors unchanged)'),
    Word(0x213ac0c, 0xa91567fa, 0xb40001e2, '  cbz  x2, Lbad              (args == NULL)'),
    Word(0x213ac10, 0xa9165ff8, 0xf9401048, '  ldr  x8, [x2, #0x20]       scalarInput'),
    Word(0x213ac14, 0xa91757f6, 0xb40001a8, '  cbz  x8, Lbad'),
    Word(0x213ac18, 0xa9184ff4, 0xb9402848, '  ldr  w8, [x2, #0x28]       scalarInputCount'),
    Word(0x213ac1c, 0xa9197bfd, 0x7100051f, '  cmp  w8, #1'),
    Word(0x213ac20, 0x910643fd, 0x54000141, '  b.ne Lbad'),
    Word(0x213ac24, 0xaa0203f5, 0xf9402449, '  ldr  x9, [x2, #0x48]       scalarOutput'),
    Word(0x213ac28, 0xaa0103f4, 0xb4000109, '  cbz  x9, Lbad'),
    Word(0x213ac2c, 0xaa0003f6, 0xb9405048, '  ldr  w8, [x2, #0x50]       scalarOutputCount'),
    Word(0x213ac30, 0xd10203b7, 0x7100051f, '  cmp  w8, #1'),
    Word(0x213ac34, 0xf0ff6cc8, 0x540000a1, '  b.ne Lbad'),
    Word(0x213ac38, 0xf946cd08, 0xd28000c8, '  mov  x8, #6                noPin | beenUnlocked'),
    Word(0x213ac3c, 0xf9400108, 0xf9000128, '  str  x8, [x9]              scalarOutput[0]'),
    Word(0x213ac40, 0xf81983a8, 0xd2800000, 'Lok:  mov  x0, #0            kIOReturnSuccess'),
    Word(0x213ac44, 0x528016b8, 0xd65f0fff, '      retab'),
    Word(0x213ac48, 0x72b04518, 0x52805840, 'Lbad: mov  w0, #0x2c2'),
    Word(0x213ac4c, 0x39023fff, 0x72bc0000, '      movk w0, #0xe000, lsl #16   kIOReturnBadArgument'),
    Word(0x213ac50, 0xf0ff6cd9, 0xd65f0fff, '      retab'),

    # Now unreachable (we return at +0x44 or +0x50 at the latest), kept so a revert of the
    # shim alone restores the previous known-good log-silencing behaviour.
    Word(0x213ae38, 0x94009766, NOP, 'externalMethod IOLog call (stops log spam)'),
]

# USB Restricted Mode bypass.
#
# Source, checked against wh1te4ever/usbliter8-fun directly: this is NOT wh1te4ever's.
# Upstream's work-27.0b2/get_boot.py (25793 bytes) has zero occurrences of it, and their
# b3 scripts are byte-identical to ours. Our fork's b2 (27361 bytes) carries it as part of
# a set of third-party additions, all commented in Vietnamese. So it was never "dropped in
# b3"; b3 never had it. Correcting my earlier claim of an upstream regression.
#
# isDeviceInRestoreMode() forced to return 1 makes the USB stack believe it is restoring,
# so USB Restricted Mode never engages and usbmux/lockdown come up before unlock or
# activation. That matters enormously here: this device has no working display, so it can
# never be unlocked, and Restricted Mode therefore blocks USB data forever. It is why the
# device has never once enumerated on a normal boot while working fine in DFU, recovery
# and SSHRD. b2's comment also warns that the DeviceTree alternative
# (disable-transport-rm=1) bricked normal boot, so do not go that route.
#
# Derivation, b2 0x2894b68 -> b4 0x28a5bdc (VA 0xfffffff0098a9bdc):
#   masked signature failed (the body diverges at a global struct offset), so this came
#   from the string anchor instead. b2's function references the boot-arg name "rd" at
#   +0x2c; that string sits in a distinctive cluster
#   ('1211...', 'rd', 'rootdev', '-restore', '%02X', '%02X ', '<print error>') which
#   appears verbatim in b4 at 0x80dbe7. Both builds have exactly 3 adrp+add references to
#   it, in the same order, and the first lands +0x2c into the function in both. The entry
#   is preceded by 0xd65f0fff (retab) in both, and 12 of the first 18 words are identical,
#   with every difference confined to relocation-sensitive fields.
KC_USB_RESTRICTED = [
    Word(0x28a5bdc, 0xd503237f, 0xd2800020, 'isDeviceInRestoreMode: mov x0, #1'),
    Word(0x28a5be0, 0xd10143ff, RET, '  ret  (bypasses USB Restricted Mode)'),
]

KC_SEP_SILENCE = [
    Word(0x2185a00, 0xd503237f, MOV_X0_0, 'sepPanicCheck'),
    Word(0x2185a04, 0xa9be4ff4, RET, '  ret'),
    Word(0x2182208, 0xd503237f, MOV_X0_0, '_didTimeout'),
    Word(0x218220c, 0xd100c3ff, RET, '  ret'),
    Word(0x2182b8c, 0xd503237f, MOV_X0_0, '_powerChangeNotificationHandler'),
    Word(0x2182b90, 0xd10543ff, RET, '  ret'),
    Word(0x2186894, 0xb4001280, NOP, '_setPowerState (a)'),
    Word(0x2185858, 0xb4000d20, NOP, '_notifyOSActiveGated'),
    Word(0x21868c4, 0xb4001120, NOP, '_setPowerState (b)'),
    Word(0x2186e4c, 0x54000180, NOP, 'PRNG reseed'),
]

# Sandbox MACF hooks -> mov x0, #0 ; ret
#
# Without these, every binary under /var/jb dies at load:
#
#   dyld: Library not loaded: @rpath/libiosexec.1.dylib
#     Reason: file system sandbox blocked mmap() of '/private/var/jb/usr/lib/libiosexec.1.dylib'
#
# which kills dpkg, apt, uicache, Sileo and the rest of the Procursus bootstrap. iOS's own
# userland is tiny (/bin holds only cat, df, ls, ps, sh), so almost everything useful lives
# in /var/jb and is unusable until these are neutered.
#
# DERIVED, not copied. Upstream publishes offsets for their 24A5370h/d421 build and also,
# usefully, the method (work-27.0b2/get_boot.py:245):
#
#   mac_policy_conf("Sandbox" / "Seatbelt sandbox policy") -> mpc_ops (+32) -> ops[index]
#   entries are chained-fixup auth pointers, target in the low 32 bits
#
# Applied to both kernelcaches:
#
#             "Sandbox"   "Seatbelt sandbox policy"   mac_policy_conf   mpc_ops
#   b2/d421   0xb32c78    0xb30e8b                    0x10feaa0         0x10fe028
#   b4/n104   0xb39519    0xb3772c                    0x10fb220         0x10fa7a8
#
# Exactly one struct in each image has a pointer to "Sandbox" at +0 and one to "Seatbelt
# sandbox policy" at +8, so there is no ambiguity about which policy we found.
#
# Three independent checks before trusting the result:
#
#  1. Running the derivation on the b2 kernelcache reproduces upstream's five published
#     offsets EXACTLY (0x2f774e0, 0x2f75640, 0x2f75474, 0x2f75110, 0x2f7019c).
#  2. The PAC discriminators in the raw pointers are identical across the two builds
#     (0x8020438d, 0x8010bd41, 0x80201586, 0x8010bcad, 0x80101042), which proves we are
#     reading the same struct slots and not coincidentally-similar tables.
#  3. Every target's first four instructions are byte-identical between b2 and b4, and all
#     start with pacibsp, so they are genuine function entries.
#
# Note the file offsets are also virtual addresses minus 0xFFFFFFF007004000: every segment
# in this kernelcache is flat-mapped (__TEXT_EXEC vm 0xfffffff008174000 -> file 0x1170000,
# and 0x8174000 - 0x7004000 == 0x1170000). Reading them with r2 against the Mach-O gives
# 0xffffffff, because r2 maps by VA; that is a tooling artefact, not bad data.
KC_SANDBOX = [
    Word(0x2F89FD8, 0xD503237F, 0xD2800000,
         "sandbox file_check_mmap (ops[36]): mov x0, #0   <- unblocks /var/jb mmap"),
    Word(0x2F89FDC, 0xA9BD6FFC, RET,
         "  ret"),
    Word(0x2F8810C, 0xD503237F, 0xD2800000,
         "sandbox mount_check_mount (ops[87]): mov x0, #0"),
    Word(0x2F88110, 0xA9BD6FFC, RET,
         "  ret"),
    Word(0x2F87F40, 0xD503237F, 0xD2800000,
         "sandbox mount_check_remount (ops[88]): mov x0, #0"),
    Word(0x2F87F44, 0xA9BC5FF8, RET,
         "  ret"),
    Word(0x2F87BDC, 0xD503237F, 0xD2800000,
         "sandbox mount_check_umount (ops[91]): mov x0, #0"),
    Word(0x2F87BE0, 0xA9BC6FFC, RET,
         "  ret"),
    Word(0x2F82C58, 0xD503237F, 0xD2800000,
         "sandbox vnode_check_rename (ops[120]): mov x0, #0"),
    Word(0x2F82C5C, 0x6DBA23E9, RET,
         "  ret"),
]

KC_SEP = KC_AKS + KC_SEP_SILENCE

KC_CREDENTIALMANAGER = [
    Word(0x2109b98, 0xd503237f, MOV_W0_0, 'sepManagerMatchedThreadCallHandler'),
    Word(0x2109b9c, 0xd10103ff, RET, '  ret'),
    Word(0x210a260, 0xd503237f, MOV_W0_0, 'callPlatformFunction'),
    Word(0x210a264, 0xd10183ff, RET, '  ret'),
    Word(0x210a2e4, 0xd503237f, MOV_W0_0, 'cmdContextV2'),
    Word(0x210a2e8, 0xd10183ff, RET, '  ret'),
    Word(0x210a36c, 0xd503237f, MOV_W0_0, 'cmdContextV3'),
    Word(0x210a370, 0xd10243ff, RET, '  ret'),
    Word(0x210a640, 0xd503237f, MOV_W0_0, 'performCommandGated'),
    Word(0x210a644, 0xd10543ff, RET, '  ret'),
    Word(0x210b1c4, 0xd503237f, MOV_W0_0, '_performKernelControl'),
    Word(0x210b1c8, 0xd10243ff, RET, '  ret'),
    Word(0x210b580, 0xd503237f, MOV_W0_0, '_performCommand'),
    Word(0x210b584, 0xd102c3ff, RET, '  ret'),
    Word(0x210b7a8, 0xd503237f, MOV_W0_0, 'processSCRDResponsePayload'),
    Word(0x210b7ac, 0xd10183ff, RET, '  ret'),
    Word(0x210b9d4, 0xd503237f, MOV_W0_0, 'scheduleDblClickDeferredAck'),
    Word(0x210b9d8, 0xd10143ff, RET, '  ret'),
    Word(0x210baf4, 0xd503237f, MOV_W0_0, 'updateAnalytics'),
    Word(0x210baf8, 0xd10183ff, RET, '  ret'),
    Word(0x210bc58, 0xd503237f, MOV_W0_0, 'performSCRDInitialization'),
    Word(0x210bc5c, 0xd10203ff, RET, '  ret'),
    Word(0x210bf1c, 0xd503237f, MOV_W0_0, 'sendSEPCommand'),
    Word(0x210bf20, 0xd10443ff, RET, '  ret'),
    Word(0x210c994, 0xd503237f, MOV_W0_0, '_setPropertiesGated'),
    Word(0x210c998, 0xd10203ff, RET, '  ret'),
    Word(0x210ce1c, 0xd503237f, MOV_W0_0, 'performDoubleClickQueryGated'),
    Word(0x210ce20, 0xd10143ff, RET, '  ret'),
    Word(0x210cf0c, 0xd503245f, MOV_W0_0, 'performLoggingLevelQueryGated'),
    Word(0x210cf10, 0xb40000c1, RET, '  ret'),
    Word(0x210d33c, 0xd503237f, MOV_W0_0, 'lockItem'),
    Word(0x210d340, 0xd101c3ff, RET, '  ret'),
    Word(0x210d568, 0xd503237f, MOV_W0_0, 'unlockItem'),
    Word(0x210d56c, 0xd10183ff, RET, '  ret'),
    Word(0x210da1c, 0xd503237f, MOV_W0_0, 'handleSEPMessage'),
    Word(0x210da20, 0xd10203ff, RET, '  ret'),
    Word(0x210dd04, 0xd503237f, MOV_W0_0, 'readFromSEPBuffer'),
    Word(0x210dd08, 0xd10183ff, RET, '  ret'),
    Word(0x210de64, 0xd503237f, MOV_W0_0, 'writeToSEPBuffer'),
    Word(0x210de68, 0xd10243ff, RET, '  ret'),
    Word(0x210e20c, 0xd503237f, MOV_W0_0, 'sendSEPMessage'),
    Word(0x210e210, 0xd10243ff, RET, '  ret'),
    Word(0x210e444, 0xd503237f, MOV_W0_0, 'clearSEPBuffer'),
    Word(0x210e448, 0xd10183ff, RET, '  ret'),
    Word(0x210e5e0, 0xd503237f, MOV_W0_0, 'getSEPEndpoint'),
    Word(0x210e5e4, 0xd10243ff, RET, '  ret'),
    Word(0x210f220, 0xd503237f, MOV_W0_0, 'powerOffActionGated'),
    Word(0x210f224, 0xd10203ff, RET, '  ret'),
    Word(0x210f4d4, 0xd503237f, MOV_W0_0, 'sepManagerMatchedGated'),
    Word(0x210f4d8, 0xd10203ff, RET, '  ret'),
    Word(0x211b7fc, 0xd503237f, MOV_W0_0, 'setPowerStateGated'),
    Word(0x211b800, 0xd101c3ff, RET, '  ret'),
]

# --- n104 / 24A5390f userland (on the System volume, patched via SSHRD) --------------
#
# coreauthd crashes on every launch with EXC_BAD_ACCESS / KERN_INVALID_ADDRESS at 0x120,
# four consecutive times, which takes SpringBoard down with it and makes launchd reboot
# the device. The offset here was not guessed: it came off the device's own crash report.
#
# Backtrace of the faulting thread, innermost first:
#     LocalAuthenticationCore  -[LACDTORatchetSEPStateParser _statusFromRatchetState:]
#     LocalAuthenticationCore  -[LACDTORatchetHandler ratchetStateWithCompletion:]
#     LocalAuthenticationCore  -[LACDTORatchetStateMonitor ratchetStateWithCompletion:]
#     LocalAuthenticationCore  -[LACDTOPendingPolicyEvaluationController _loadPendingEvaluations]
#     LocalAuthenticationCore  -[LACDTOPendingPolicyEvaluationController startController]
#     coreauthd               +0x95c4          <- return address, so the BL is at 0x95c0
#
# With SEP dead the ratchet state is never populated, so the parser dereferences null.
# Nopping the call skips the whole DTO ratchet parse. Verified: 0x95c0 is
# `bl 0x3d9e0`, a branch into __objc_stubs (0x3a780-0x3de20), and 0x95c4 is the
# `ldr x0, [sp, 8]` the backtrace returns to.
COREAUTHD = [
    Word(0x95C0, 0x9400D108, NOP,
         "was bl objc_msgSend$startController; skips the SEP DTO-ratchet parse"),
]

COREAUTHD_BIN = ("coreauthd", 467744,
                 "771030de3f404e212e3f17cbea3004ccf209d95378012de7ee595c3219ef9084")

# mobileactivationd: offline hacktivation. The device has fm-activation-locked=YES and no
# activation record, so Setup waits forever on an activation that cannot complete with SEP
# dead and no network.
#
# -[DeviceType should_hactivate] is a two-instruction BOOL getter:
#     ldrb w0, [x0, 0x14]
#     ret
# Forcing it to return 1 makes mobileactivationd log "Hactivation is enabled, short
# circuiting activation state to Activated." and report the device as activated.
#
# Located by resolving the selector through the ObjC class list (objc_imp.py), not by
# offset arithmetic: -[DeviceType should_hactivate] at file offset 0x2ec2d8. radare2
# independently labels that address method.DeviceType.should_hactivate.
#
# The second set, in -[MobileActivationDaemon getActivationStateWithCompletionBlock:], was
# deferred on the first pass ("test the simple one alone first") and then never revisited.
# It is applied now. should_hactivate only changes what the daemon DECIDES; this set changes
# what it REPORTS when something asks the device its activation state, which is a different
# code path and is what upstream's README lists alongside it.
#
# Derivation, all four sites (upstream's b2 offsets do not transfer, the binary shifted):
#   -[MobileActivationDaemon getActivationStateWithCompletionBlock:] resolved through the
#   ObjC class list to file offset 0x329b00 (entry word pacibsp). The body reads the state
#   out of data_ark and falls back to kMAActivationStateUnactivated:
#
#       0x329b60  tbz  w22, 0, ...        <- dataMigrationCompleted gate
#         ... 0x60 later ...
#       0x329bc0  adrp x8, 0x1003c9000    \
#       0x329bc4  add  x8, x8, 0x310       |  load kMAActivationStateUnactivated
#       0x329bc8  ldr  x0, [x8]           /
#
#   Upstream's b2 sites are 0x327CB0 then 0x327D10/D14/D18: the SAME 0x60 gap, and the same
#   three-instruction load rewritten as adrp/add/NOP. That spacing is what identifies the
#   sites, not the absolute offsets.
#
#   The CFString "Activated" object is at 0x3eb2a8, found by locating the C string at
#   0x3b8181 and then the 32-byte __cfstring whose cstr pointer resolves to it and whose
#   length field is 9. The adrp/add pair was produced by adrp_xref.py, which reproduces
#   upstream's published b2 words exactly (differing only in Rd), and self-verifies that
#   the pair resolves to 0x3eb2a8.
MOBILEACTIVATIOND = [
    Word(0x2EC2D8, 0x39405000, 0x52800020,
         "-[DeviceType should_hactivate]: was ldrb w0,[x0,0x14]; now mov w0,#1"),
    Word(0x329B60, 0x36000576, NOP,
         "getActivationState: was tbz w22,0 (dataMigrationCompleted gate)"),
    Word(0x329BC0, 0x90000508, 0xD0000600,
         '  was adrp x8, 0x1003c9000; now adrp x0, page of CFString "Activated"'),
    Word(0x329BC4, 0x910C4108, 0x910AA000,
         "  was add x8, x8, #0x310; now add x0, x0, #0x2a8  -> 0x3eb2a8"),
    Word(0x329BC8, 0xF9400100, NOP,
         "  was ldr x0, [x8] (kMAActivationStateUnactivated)"),
]

# ctkd: -[TKSEPKeyServer serverAttributesOfKey:error:] -> return nil.
#
# Without it, -[TKLocalSEPRefKey keyType] raises an uncaught NSException against our fake
# SEP. It has not crashed on this build, which is why it was never applied, but upstream
# lists it alongside coreauthd and mobileactivationd as part of getting past Setup.
#
# Derivation: resolved through the ObjC class list to file offset 0x1b38, which is the
# ONLY class implementing that selector. Entry word is pacibsp and the word before it is
# retab, so it is a genuine function entry. The offset happens to be identical to
# upstream's b2 value, which is plausible for a 321 KB binary with this method near the
# start, but it was derived independently rather than copied.
CTKD = [
    Word(0x1B38, 0xD503237F, MOV_X0_0,
         "-[TKSEPKeyServer serverAttributesOfKey:error:]: mov x0, #0"),
    Word(0x1B3C, 0xD10303FF, RET, "  ret  (return nil)"),
]

MOBILEACTIVATIOND_BIN = ("mobileactivationd", 4639984,
                         "1d7eab7f4e936e7c32f86803fdbb1e00be52a123e81498ff3d0dd304a2c81c14")

CTKD_BIN = ("ctkd", 321616,
            "94bc04c3de081ed4325b21e8965d8eca490d6286dacb54856e5f2de6c15cefc1")

# Pristine identity of each d79 / 24A5390f payload. A table refuses to run unless the
# file is exactly this size. Without this, a table can partially "verify" against an
# unrelated binary: the zeroed-scratch check in particular passes on any file that happens
# to hold zeroes at that offset, which TXM does at 0xd0e30.
#
# Size is the hard gate. The hash is reported so you can tell pristine from already-patched.
IBSS = ("d79 iBSS/iBEC payload", 2481856,
        "2749ee482b68342df82ec48f37148c29c70fc9d347d0b20005a6ff6c2d5b8f71")
TXM = ("TXM payload", 540704,
       "75a6b02c1a2d0f43bfe85942b5c205e2e9afe692231de0d361dbc85d4fc63cf9")
RESTORED = ("restored_external", 3340448,
            "f54aaf9e75484177201281fd746d24f9bb4b057661e2fda77a7f051266e410f4")
ASR_BIN = ("asr", 380192,
           "8d0bb0cf174b4739045a85b14f5835598182f64cf3b67530bba8a4ac3d8a226f")

# --- iBEC display diagnostics: is pinot_init returning 0 or -1? ----------------------
#
# These are DIAGNOSTICS, not fixes. They are not part of any boot table and must be
# selected by name. Apply them on top of an already-"ibss-normal"-patched iBEC.raw; the
# offsets do not collide with VALIDATE (0x23728/0x2372c) or bootargs (0x2aa28/0x2aa2c
# and the blob at 0xd0e30), so the sha256 line will read MODIFIED and that is expected.
#
# Background is in work-27.0b4-n104/README.md, display section. In short:
#
#   pinot_init is 0x9e268, reached from the generic display wrapper through slot 0 of
#   the function table at 0x1302d8. It returns 0 or -1 and nothing else. The -1 has
#   exactly one predecessor in the whole function:
#
#       0x9e500  ldr w8, [x25, 0xab0]   ; state->panel_id
#       0x9e504  cbz w8, 0x9e684        ; == 0 -> bl 0x1f8e4 ; mov w0, -1 ; ret
#
#   panel_id is filled from, in order: a value cached by an earlier call, the env var
#   "pinot-panel-id", or a MIPI DSI Generic Read (data type 0x14) of panel register 0xb1
#   at 0x9e42c. A failed or short read leaves it 0, so a failed read is fatal. There is
#   no fallback configuration path, which rules out the "tolerated failure still returns
#   0" theory for this build.
#
#   The wrapper then does cbz w0, 0x96b10 and on non-zero zeroes the display object out.
#
# We cannot currently observe which of the two happened. Every value we were reading back
# with `irecovery -s` is an INPUT: backlight-level, display-timing and display-color-space
# are all fetched by env readers (0xa6470 / 0xa634c), never written by the display path.
# backlight-level's 1435 is an NVRAM leftover from the device's last normal iOS session;
# the name is an entry in iBoot's env-var table at 0x119680. So these two patches exist to
# turn that unknown into an observation.
#
# Run them one at a time, never together, and keep an unpatched control boot for
# comparison. The three cases are:
#
#   A  no diagnostic patch          real MIPI read, real failure behaviour (the control)
#   B  ibec-diag-ignore-pinot-id-failure
#   C  ibec-diag-force-pinot-id     supply an ID, skip the MIPI read entirely
#
# --- B ---
# Redirect the failure branch to the known success return rather than NOPing it.
#
# NOPing 0x9e504 would let a zero panel_id fall into 0x9e508, the full bring-up sequence,
# which normally only ever runs with a valid ID. That adds a second unknown ("what does
# bring-up do when handed 0?") on top of the one we are trying to measure. Redirecting to
# 0x9e67c instead changes exactly one thing: the failure stops being reported as failure.
#
#   0x9e67c  mov w0, 0
#   0x9e680  b   0x9e68c        -> shared epilogue, retab
#
# 0x9e67c is safe to enter directly. It is inside the same frame, sets w0, and falls into
# the epilogue that restores x19-x28/x29/x30 and adds 0x90 to sp. Nothing between 0x9e504
# and 0x9e67c is required for that epilogue to be correct.
#
# A side effect worth knowing: this also skips `bl 0x1f8e4` on the failure path. 0x1f8e4
# writes 0x1fffff to a DSIM register (base 0x230400a0, +0x24) and is almost certainly the
# mipi_dsim_quiesce() seen in the historical n104 log. Skipping it leaves the DSIM powered
# rather than shut down, which for this experiment is what we want.
#
# Reading the result:
#   any change at all, including backlight flicker or a different black level
#       -> the Pinot failure/teardown path is involved, i.e. it was returning -1
#   no change whatsoever
#       -> does NOT prove the read succeeded. It only proves that keeping the display
#          object alive is not by itself sufficient. Panel programming may still need a
#          real ID, which is what C is for.
DISPLAY_IGNORE_PINOT_ID_FAILURE = [
    Word(0x9E504, 0x34000C08, 0x34000BC8,
         "was cbz w8, 0x9e684 (quiesce, return -1); now cbz w8, 0x9e67c (return 0)"),
]


# --- C ---
# Supply a panel ID directly and skip the MIPI read.
#
#   0x9e3d4  mov x4, #0        ; the DEFAULT handed to env_get_uint
#   0x9e3d8  bl  0xa6470       ; env_get_uint("pinot-panel-id", 0)
#   0x9e3dc  str w0, [x25, 0xab0]
#   0x9e3e0  cbnz w0, 0x9e4a8  ; non-zero -> straight to decode, MIPI read never happens
#
# Overwriting the default alone is not enough, because MOVZ carries only 16 bits and a
# panel ID is 32 (bits 24..31 are reply byte 0). So both words go: the constant is built
# in w0 with MOVZ+MOVK and the env lookup is dropped. 0x9e3dc and 0x9e3e0 are untouched
# and do the rest. The dead x0..x3 setup at 0x9e3bc..0x9e3d0 is harmless: w0 is
# overwritten here and the decode at 0x9e4a8 only reads w0 and x8.
#
# This one needs a real panel ID from this exact panel, which we do NOT have. Do not
# invent one. The layout, decoded from 0x9e444..0x9e484, is:
#
#   bits  0..2   reply[2] & 7          bits  8..11  (reply[2] >> 4) & 0xf
#   bits  3..5   reply[3] & 7          bits 12..15  (reply[3] >> 4) & 0xf
#   bit   6      (reply[3] >> 3) & 1   bits 16..23  reply[1]
#   bit   7      (reply[2] >> 3) & 1   bits 24..31  reply[0]
#
# and (panel_id & 0x3f) == 8 is the case that triggers the extra decode at 0x9e4b4, which
# means reply[2] & 7 == 0 and reply[3] & 7 == 1.
def force_pinot_id(value):
    """Build the two-word patch that hardcodes panel_id and skips the MIPI read."""
    lo, hi = value & 0xFFFF, (value >> 16) & 0xFFFF
    return [
        Word(0x9E3D4, 0xD2800004, 0x52800000 | (lo << 5),
             f"was mov x4, #0 (env_get_uint default); now movz w0, #{lo:#06x}"),
        Word(0x9E3D8, 0x94002026, 0x72A00000 | (hi << 5),
             f"was bl 0xa6470 (env_get_uint); now movk w0, #{hi:#06x}, lsl 16"),
    ]


# --- D ---
# Stop iBSS initialising the display, so iBEC is the first thing to touch the panel.
#
# This exists because of the sharpest fact the A/B run produced: iBSS and iBEC are the
# SAME BYTES on this build (both 2563720, sha256 6cf70de6... pristine, 8c9633d2... after
# ibss-normal), yet iBSS's display init succeeds and paints dark blue while iBEC's fails
# the panel-ID read. Same code, same patches, opposite outcome, so the difference is
# execution context. The obvious candidate is order: iBSS gets there first and leaves the
# panel initialised and running, and a DSI read may not be serviceable in that state.
#
# Top-level display init:
#
#   0x351c4  bl 0x4d788        ; power on the DSIM gate (0x71)
#   0x351c8  bl 0x9679c        ; the display init wrapper, which contains the blraaz to
#                             ; pinot_init at 0x96af4 and its return sites at 0x96a8c/0x96dd4
#   0x351cc  cbz w0, 0x3517c   ; 0 = success -> mark done and register backlight-level
#   0x351d0  mov w8, 1
#   0x351d4  strb w8, [x20, 0xbfb]  ; else mark "attempted and failed"
#   0x351d8  mov w0, 0         ; either way the caller sees 0
#
# Replacing the call with `movz w0, #1` means the wrapper is never entered and the
# non-zero falls through to the already-existing failed path, which is a state the code
# handles by design. Nothing is left half-initialised: 0x351c4 still powers the DSIM gate
# on, which is harmless and iBEC does it again anyway.
#
# APPLY TO iBSS ONLY. Applying it to iBEC would disable the very thing we are measuring.
IBSS_SKIP_DISPLAY_INIT = [
    Word(0x351C8, 0x94018575, 0x52800020,
         "was bl 0x9679c (display init); now movz w0, #1 -> take the failed path"),
]


# --- d79 / 24A5390f kernel relocation -----------------------------------------------
# d79 and n104 use the same T8030 code families, but their fileset layouts differ.
# Every destination was located by instruction anchors and checked against the pristine
# d79 payload. Five instructions encode layout-dependent addresses, so their d79
# originals are recorded explicitly instead of weakening the byte guard.
def relocate(patches, delta=0, offsets=None, orig_overrides=None):
    """Clone a patch list at d79 offsets while preserving original-byte guards."""
    offsets = offsets or {}
    orig_overrides = orig_overrides or {}
    out = []
    for item in patches:
        off = offsets.get(item.off, item.off + delta)
        if isinstance(item, Word):
            out.append(Word(off, orig_overrides.get(item.off, item.orig), item.new,
                            item.comment))
        elif isinstance(item, Text):
            out.append(Text(off, item.orig, item.payload, item.comment))
        else:
            raise TypeError(f"kernel relocation does not support {type(item).__name__}")
    return out


D79_KC_KERNELNAME = relocate(KC_KERNELNAME)
D79_KC_PANICS = relocate(KC_PANICS, offsets={
    0x3052800: 0x2FB9BC0,
    0x2FC0CEC: 0x2F280AC,
    0x36D5B48: 0x363DB48,
    0x3053C10: 0x2FBAFD0,
})
D79_KC_AMFI = relocate(KC_AMFI, delta=-0x40F00, offsets={
    0x3A1C748: 0x3984748,
    0x3A1C74C: 0x398474C,
    0x376351C: 0x36CB51C,
    0x3763520: 0x36CB520,
}, orig_overrides={
    0x3A1C748: 0x90FE9628,
    0x376351C: 0xB0FEA948,
})
D79_KC_PERSONA = relocate(KC_PERSONA, delta=-0x98000)
D79_KC_AKS = relocate(KC_AKS, delta=-0x80F10, orig_overrides={
    0x213AC34: 0xD0FF6FA8,
    0x213AC38: 0xF9447108,
    0x213AC50: 0xD0FF6FB9,
})
D79_KC_SEP_SILENCE = relocate(KC_SEP_SILENCE, delta=-0x80F10)
D79_KC_SEP = D79_KC_AKS + D79_KC_SEP_SILENCE
D79_KC_CREDENTIALMANAGER = relocate(KC_CREDENTIALMANAGER, delta=-0x80F10)
D79_KC_USB_RESTRICTED = relocate(KC_USB_RESTRICTED, delta=-0x98C40)
D79_KC_SANDBOX = relocate(KC_SANDBOX, delta=-0x98C40)


TABLES = {
    "kc-restore": (KC, D79_KC_KERNELNAME + D79_KC_PANICS + D79_KC_AMFI),
    # KC_USB_RESTRICTED is normal-boot only. The restore chain already boots with rd=md0,
    # so isDeviceInRestoreMode() is genuinely true there and needs no help.
    # KC_SANDBOX is normal-boot only. The restore chain never touches /var/jb, and upstream
    # likewise applies these in get_boot.py and not in make_cfw.py.
    "kc-boot": (KC, D79_KC_KERNELNAME + D79_KC_PANICS + D79_KC_AMFI
                + D79_KC_PERSONA + D79_KC_SEP + D79_KC_CREDENTIALMANAGER
                + D79_KC_USB_RESTRICTED + D79_KC_SANDBOX),
    # Diagnostic build. Keeps the anti-hang AKS fixes but omits the panic silencers and
    # the whole AppleCredentialManager block, so a failure panics and writes a log to
    # /private/var/logs/CrashReporter/Panics naming the exact check, instead of hanging
    # silently. Not expected to boot; expected to explain why it does not.
    "kc-diag": (KC, D79_KC_KERNELNAME + D79_KC_PANICS + D79_KC_AMFI + D79_KC_AKS),
    "txm-restore": (TXM, TXM_QUERYMODULE + TXM_CONSTRAINTS),
    "txm-boot": (TXM, TXM_QUERYMODULE + TXM_CONSTRAINTS + TXM_SECURECHANNEL + TXM_DEVMODE),
    "ibss-restore": (IBSS, VALIDATE + bootargs("-v wdt=-1 rd=md0 -restore")),
    "ibss-ramdisk": (IBSS, VALIDATE + bootargs("rd=md0 -v wdt=-1 debug=0x2014e")),
    # serial=3 dropped: it routes the console to a UART we have no cable for, so it can
    # only hide output, never produce it.
    #
    # backlight-level added by hand. iBEC normally appends "backlight-level=%d" itself at
    # 0x2aab4, but that whole block is gated at 0x2aa8c on a display-object lookup
    # (0xb1658 -> 0xb16a8) returning non-NULL. On this device the display never comes up
    # in iBEC, the lookup returns NULL, and the kernel is handed boot-args with no
    # backlight level at all. n104 is an LCD and needs its backlight driven explicitly,
    # unlike the OLED d421 this chain was written for, so that omission alone is enough to
    # leave the panel dark with a perfectly healthy framebuffer behind it. Since we own
    # the whole string we can supply the value directly and skip the predicate.
    # ~~launchd_no_cache=1, NOT upstream's launchd_unsecure_cache=1. The latter does not
    # exist in iOS 27: grepping the device's own /sbin/launchd returns 0 matches for it
    # while known strings return 8, so the search is sound and the flag is simply gone.~~
    #
    # WRONG, corrected 2026-08-07. The search looked in the wrong binary. There are two
    # separate boot arguments living in two separate programs:
    #
    #   /sbin/launchd                      launchd_no_cache=        1 match, unsecure 0
    #   /usr/libexec/launchd_cache_loader  launchd_unsecure_cache=  1 match, no_cache 0
    #
    # We grepped only /sbin/launchd, found no "launchd_unsecure_cache", and concluded the
    # flag was gone from iOS 27. It is not gone; it belongs to the cache loader. The
    # loader's own strings settle it:
    #
    #   0x1d30  "Using default cache paths"      <- what our launchd_cache_loader.log showed
    #   0x1dce  "launchd_unsecure_cache="
    #   0x1de6  "Using unsecure cache: %s"       <- the path we never reached
    #
    # So upstream's flag was right all along and ours silently selected the signed-cache
    # path. That is very likely why no daemon we placed was ever started, and why we ended
    # up hijacking a cached job instead.
    #
    # Switching to upstream's flag. Expected evidence on the next boot is "Using unsecure
    # cache:" in /var/log/com.apple.xpc.launchd/launchd_cache_loader.log instead of "Using
    # default cache paths". If it still says default, the loader recognises the argument
    # but refuses the branch, and the next step is reversing the loader around 0x1dce
    # rather than transplanting anyone else's offset.
    "ibss-normal": (IBSS, VALIDATE + bootargs(
        "-v debug=0x2014e launchd_unsecure_cache=1 wdt=-1 backlight-level=1024")),
    # n104 display and Pinot-ID patches are intentionally absent for d79: their offsets
    # and panel assumptions are board-specific and have not been derived here.
    "restored_external": (RESTORED, RESTORED_EXTERNAL),
    "asr": (ASR_BIN, ASR),
    "coreauthd": (COREAUTHD_BIN, COREAUTHD),
    "mobileactivationd": (MOBILEACTIVATIOND_BIN, MOBILEACTIVATIOND),
    "ctkd": (CTKD_BIN, CTKD),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("table", nargs="?", help="which patch set to apply")
    ap.add_argument("binary", nargs="?", help="raw payload to patch")
    ap.add_argument("-o", "--output", help="write result here (omit for dry run)")
    ap.add_argument("--list", action="store_true", help="list available patch tables")
    ap.add_argument("--pinot-id", type=lambda s: int(s, 0), metavar="N",
                    help="32-bit panel id for ibec-diag-force-pinot-id (accepts 0x...)")
    args = ap.parse_args()

    if args.list or not args.table:
        print("patch tables:")
        for k, (ident, v) in TABLES.items():
            print(f"  {k:34s} {len(v):2d} patches   target: {ident[0]} ({ident[1]} bytes)")
        return 0
    if args.table not in TABLES:
        sys.exit(f"[err] unknown table {args.table!r}; try --list")
    if not args.binary:
        sys.exit("[err] need a binary")

    (want_name, want_size, want_sha), patches = TABLES[args.table]

    # This table is parameterised, so the registered copy is only a placeholder that makes
    # --list report the right patch count. Refuse to run on the placeholder: forcing the
    # id to 0 would leave cbnz w0 untaken and silently fall through to the MIPI read,
    # which looks like the control boot and would be read as a result.
    if args.table == "ibec-diag-force-pinot-id":
        if args.pinot_id is None:
            sys.exit("[err] ibec-diag-force-pinot-id needs --pinot-id N (the 32-bit panel "
                     "id).\n      We do not have a known-good id for this panel; do not "
                     "invent one.")
        if not 0 < args.pinot_id <= 0xFFFFFFFF:
            sys.exit(f"[err] --pinot-id must be a non-zero 32-bit value, got "
                     f"{args.pinot_id:#x}. Zero would not skip the MIPI read.")
        patches = force_pinot_id(args.pinot_id)
    elif args.pinot_id is not None:
        sys.exit(f"[err] --pinot-id means nothing for table {args.table!r}")

    data = Path(args.binary).read_bytes()
    print(f"[*] {args.table}: {len(patches)} patches against {Path(args.binary).name} "
          f"({len(data)} bytes)")

    # Identity gate. Wrong file, wrong build: stop before looking at any offset.
    if len(data) != want_size:
        sys.exit(f"[err] this table targets {want_name} ({want_size} bytes), "
                 f"but {Path(args.binary).name} is {len(data)} bytes. Wrong file.")
    got_sha = hashlib.sha256(data).hexdigest()
    state = "pristine" if got_sha == want_sha else "MODIFIED (already patched?)"
    print(f"    target {want_name}, size OK, sha256 {got_sha[:16]}... {state}\n")

    failed = skipped = 0
    for p in patches:
        ok, detail = p.check(data)
        mark = {True: "OK  ", None: "SKIP", False: "FAIL"}[ok]
        print(f"    {mark}  {p.off:#08x}  {detail}")
        print(f"          {p.comment}")
        failed += ok is False
        skipped += ok is None

    if failed:
        sys.exit(f"\n[err] {failed} patch(es) failed verification, nothing written.\n"
                 f"      The bytes at those offsets are not what this table expects. "
                 f"Wrong build or wrong offset.")

    print(f"\n[+] all {len(patches)} verified" + (f" ({skipped} already applied)" if skipped else ""))
    if not args.output:
        print("[dry-run] no output path given, nothing written.")
        return 0

    buf = bytearray(data)
    for p in patches:
        p.apply(buf)
    Path(args.output).write_bytes(buf)

    # Re-read and confirm every patch is now present.
    check = Path(args.output).read_bytes()
    bad = []
    for p in patches:
        if p.kind == "word":
            if struct.unpack_from("<I", check, p.off)[0] != p.new:
                bad.append(p)
        elif check[p.off:p.off + len(p.payload)] != p.payload:
            bad.append(p)
    for p in bad:
        print(f"[FAIL] {p.off:#x} did not take")
    print(f"[+] wrote {args.output}  verify={'OK' if not bad else 'FAIL'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
