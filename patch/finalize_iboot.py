#!/usr/bin/env python3
"""Finalize Leeksov-patched iBEC: rd=md0 boot-args + board-safe IMG4 wrappers."""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

# Verified n841 / iBoot-11881 remote-boot wrapper (XR 18.7.9).
N841_FALSE_POSITIVE_OFFSET = 0xE10
N841_FALSE_POSITIVE_LENGTH = 8
N841_IMAGE4_CANARY_BRANCH = 0x2C194
N841_IMAGE4_CALLBACK_RESULT = 0x2C198
N841_UPDATE_DEVICE_TREE_TRAMPOLINE = 0x2C8DC

# Early false-positive epilogues the Leeksov heuristic hits first.
# 18.1 d321: 0x10CC; 18.6+ d321: 0xE10 (same shape as n841).
D321_EARLY_FALSE_POSITIVES = (0xE10, 0x10CC)

# d79 / mBoot-18000.122.4 (iOS 26.5.2 23F84).
#
# The shared "%s" string beside "rd=md0" has four ADRP+ADD references in
# this image.  The generic patchfinder redirects all four, but only the first
# belongs to the restore boot-argument builder.  The others format AMCC
# interrupt errors, NVRAM values, and exception diagnostics.  In particular,
# corrupting the NVRAM formatter can alter the value written by setenvnp
# immediately before bootx.
D79_IMAGE4_CANARY_BRANCH = 0x20650
D79_IMAGE4_CALLBACK_RESULT = 0x20654
D79_BOOT_ARGS_REFERENCE = 0x23EEC
D79_FALSE_BOOT_ARGS_REFERENCES = (0x430D4, 0xA0558, 0xE3534)
D79_IBOOT_BASE = 0x870000000
D79_EXPECTED_BUILD_TAG = b"mBoot-18000.122.4"
D79_EXPECTED_BUILD_TAG_COUNT = 2

NOP = bytes.fromhex("1f2003d5")
MOV_X0_ZERO = bytes.fromhex("000080d2")

# Leeksov iboot_patchfinder writes this slot (29 bytes incl. NUL).
# Finalize replaces it with rd=md0 *without* trailing "%s" — the Jul 23
# working XS iBoot had no %s; baking "%s" left a literal format token and
# broke NAND bring-up (disk0 not configured / mount failures).
LEEKSOV_BOOT_ARGS = b"serial=3 -v debug=0x2014e %s\x00"
RAMDISK_BOOT_ARGS = b"rd=md0 -v debug=0x2014e\x00\x00\x00\x00\x00\x00"


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _collect_canary_sites(data: bytes) -> list[int]:
    """Same B.NE + MOV + CMP + MOVN heuristic as iboot_patchfinder."""
    sites: list[int] = []
    limit = min(len(data) - 8, 0x200000)
    for off in range(0, limit, 4):
        w = _u32(data, off)
        if (w & 0xFF00001F) != 0x54000001:  # B.NE
            continue
        nxt = _u32(data, off + 4)
        if (nxt & 0xFFE0FFE0) != 0xAA0003E0:  # MOV X0, Xn
            continue
        src_reg = (nxt >> 16) & 0x1F
        has_cmp = any(
            (_u32(data, off - k * 4) & 0xFFE0FC1F) == 0xEB00001F
            or (_u32(data, off - k * 4) & 0x7F20001F) == 0x6B00001F
            for k in range(1, 9)
            if off - k * 4 >= 0
        )
        if not has_cmp:
            continue
        has_movn = False
        for k in range(1, 65):
            if off - k * 4 < 0:
                break
            prev = _u32(data, off - k * 4)
            if (prev & 0xFFE0001F) == (0x12800000 | src_reg):
                has_movn = True
                break
        if has_movn:
            sites.append(off)
    return sites


def _asn1_anchors(data: bytes, base: int = 0x870000000) -> list[int]:
    asn1 = data.find(b"Unknown ASN1 type")
    if asn1 < 0:
        return []
    anchors: list[int] = []
    # ADR
    for off in range(0, min(len(data) - 4, 0x200000), 4):
        w = _u32(data, off)
        if (w & 0x9F000000) != 0x10000000:
            continue
        immhi = (w >> 5) & 0x7FFFF
        immlo = (w >> 29) & 0x3
        imm = (immhi << 2) | immlo
        if imm & (1 << 20):
            imm -= 1 << 21
        if (base + off) + imm == base + asn1:
            anchors.append(off)
    # ADRP + ADD
    asn1_page = (base + asn1) & ~0xFFF
    page_off = (base + asn1) & 0xFFF
    for off in range(0, min(len(data) - 8, 0x200000), 4):
        w = _u32(data, off)
        if (w & 0x9F000000) != 0x90000000:
            continue
        immhi = (w >> 5) & 0x7FFFF
        immlo = (w >> 29) & 0x3
        imm = (immhi << 2) | immlo
        if imm & (1 << 20):
            imm -= 1 << 21
        page = ((base + off) & ~0xFFF) + (imm << 12)
        if page != asn1_page:
            continue
        rd = w & 0x1F
        for d in range(4, 20, 4):
            add_off = off + d
            aw = _u32(data, add_off)
            if (aw & 0xFF800000) != 0x91000000:
                continue
            if ((aw >> 5) & 0x1F) != rd:
                continue
            if ((aw >> 10) & 0xFFF) == page_off:
                anchors.append(off)
                break
    return anchors or [asn1]


def find_d321_image4_canary(stock: bytes) -> int:
    sites = _collect_canary_sites(stock)
    if not sites:
        raise SystemExit("d321: no image4 canary heuristic sites")
    anchors = _asn1_anchors(stock)
    best = sites[0]
    best_dist = 10**9
    for off in sites:
        for a in anchors:
            dist = abs(off - a)
            if dist < best_dist:
                best_dist = dist
                best = off
    if best_dist >= 0x1000:
        raise SystemExit(
            f"d321: no ASN1-near image4 canary (best 0x{best:X} dist 0x{best_dist:X})"
        )
    return best


def apply_boot_args(data: bytearray) -> int:
    if data.count(RAMDISK_BOOT_ARGS) == 1:
        print("boot-args already set to ramdisk form")
        return data.index(RAMDISK_BOOT_ARGS)
    if len(LEEKSOV_BOOT_ARGS) != len(RAMDISK_BOOT_ARGS):
        raise SystemExit("internal boot-args length mismatch")
    if data.count(LEEKSOV_BOOT_ARGS) != 1:
        raise SystemExit(
            "expected exactly one Leeksov boot-args slot "
            f"({LEEKSOV_BOOT_ARGS!r}); found {data.count(LEEKSOV_BOOT_ARGS)}"
        )
    idx = data.index(LEEKSOV_BOOT_ARGS)
    data[idx : idx + len(LEEKSOV_BOOT_ARGS)] = RAMDISK_BOOT_ARGS
    print(f"boot-args → rd=md0 @ 0x{idx:X}")
    return idx


def _decode_adrp_add_target(data: bytes | bytearray, off: int) -> int | None:
    if off < 0 or off + 8 > len(data):
        return None
    adrp = _u32(data, off)
    add = _u32(data, off + 4)
    if (adrp & 0x9F000000) != 0x90000000:
        return None
    rd = adrp & 0x1F
    if (add & 0xFF800000) != 0x91000000 or ((add >> 5) & 0x1F) != rd:
        return None
    immhi = (adrp >> 5) & 0x7FFFF
    immlo = (adrp >> 29) & 0x3
    page_delta = (immhi << 2) | immlo
    if page_delta & (1 << 20):
        page_delta -= 1 << 21
    page = ((D79_IBOOT_BASE + off) & ~0xFFF) + (page_delta << 12)
    add_imm = (add >> 10) & 0xFFF
    if (add >> 22) & 1:
        add_imm <<= 12
    return page + add_imm - D79_IBOOT_BASE


def apply_n841_wrapper(stock: bytes, patched: bytearray) -> None:
    if len(stock) != len(patched):
        raise SystemExit("stock and patched iBoot sizes differ")
    patched[
        N841_FALSE_POSITIVE_OFFSET : N841_FALSE_POSITIVE_OFFSET
        + N841_FALSE_POSITIVE_LENGTH
    ] = stock[
        N841_FALSE_POSITIVE_OFFSET : N841_FALSE_POSITIVE_OFFSET
        + N841_FALSE_POSITIVE_LENGTH
    ]
    patched[N841_IMAGE4_CANARY_BRANCH : N841_IMAGE4_CANARY_BRANCH + 4] = NOP
    patched[N841_IMAGE4_CALLBACK_RESULT : N841_IMAGE4_CALLBACK_RESULT + 4] = MOV_X0_ZERO
    patched[
        N841_UPDATE_DEVICE_TREE_TRAMPOLINE : N841_UPDATE_DEVICE_TREE_TRAMPOLINE + 4
    ] = stock[
        N841_UPDATE_DEVICE_TREE_TRAMPOLINE : N841_UPDATE_DEVICE_TREE_TRAMPOLINE + 4
    ]
    print("applied n841ap safe IMG4 / UpdateDeviceTree wrapper")


def apply_d321_wrapper(stock: bytes, patched: bytearray) -> None:
    if len(stock) != len(patched):
        raise SystemExit("stock and patched iBoot sizes differ")
    # Undo early false-positive epilogues (build-dependent: 0xE10 and/or 0x10CC).
    for fp in D321_EARLY_FALSE_POSITIVES:
        if fp + 8 <= len(stock):
            patched[fp : fp + 8] = stock[fp : fp + 8]
    site = find_d321_image4_canary(stock)
    patched[site : site + 4] = NOP
    patched[site + 4 : site + 8] = MOV_X0_ZERO
    print(
        "applied d321ap safe IMG4 wrapper "
        f"(real canary @ 0x{site:X}, restored early FPs {[hex(x) for x in D321_EARLY_FALSE_POSITIVES]})"
    )


def apply_d79_wrapper(stock: bytes, patched: bytearray, boot_args_slot: int) -> None:
    if len(stock) != len(patched):
        raise SystemExit("stock and patched iBoot sizes differ")
    if stock.count(D79_EXPECTED_BUILD_TAG) != D79_EXPECTED_BUILD_TAG_COUNT:
        raise SystemExit(
            "d79: wrapper is pinned to mBoot-18000.122.4; "
            "refusing an unverified iBoot build"
        )

    bl = _u32(stock, D79_IMAGE4_CANARY_BRANCH - 4)
    bne = _u32(stock, D79_IMAGE4_CANARY_BRANCH)
    mov = _u32(stock, D79_IMAGE4_CALLBACK_RESULT)
    if (bl >> 26) != 0b100101:
        raise SystemExit("d79: expected BL before outlined IMG4 canary")
    if (bne & 0xFF00001F) != 0x54000001:
        raise SystemExit("d79: expected B.NE at outlined IMG4 canary")
    if (mov & 0xFFE0FFE0) != 0xAA0003E0:
        raise SystemExit("d79: expected MOV X0, Xn after outlined IMG4 canary")

    original_targets = {
        _decode_adrp_add_target(stock, off)
        for off in (D79_BOOT_ARGS_REFERENCE, *D79_FALSE_BOOT_ARGS_REFERENCES)
    }
    if len(original_targets) != 1 or None in original_targets:
        raise SystemExit("d79: expected all four stock references to share one %s string")
    original_target = next(iter(original_targets))
    if stock[original_target : original_target + 3] != b"%s\0":
        raise SystemExit(
            f"d79: shared stock reference at 0x{original_target:X} is not %s"
        )

    patched[D79_IMAGE4_CANARY_BRANCH : D79_IMAGE4_CANARY_BRANCH + 4] = NOP
    patched[D79_IMAGE4_CALLBACK_RESULT : D79_IMAGE4_CALLBACK_RESULT + 4] = MOV_X0_ZERO

    target = _decode_adrp_add_target(patched, D79_BOOT_ARGS_REFERENCE)
    if target != boot_args_slot:
        rendered = "none" if target is None else f"0x{target:X}"
        raise SystemExit(
            "d79: boot-args reference does not target the injected slot "
            f"(got {rendered}, expected 0x{boot_args_slot:X})"
        )

    for off in D79_FALSE_BOOT_ARGS_REFERENCES:
        patched[off : off + 8] = stock[off : off + 8]

    restored = ", ".join(f"0x{off:X}" for off in D79_FALSE_BOOT_ARGS_REFERENCES)
    print(
        "applied d79ap 23F84 safe IMG4 / boot-args wrapper "
        f"(kept 0x{D79_BOOT_ARGS_REFERENCE:X}, restored {restored})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stock", required=True, type=Path)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--board", required=True, help="DeviceClass / boardconfig")
    args = parser.parse_args()

    stock = args.stock.read_bytes()
    patched = bytearray(args.input.read_bytes())
    boot_args_slot = apply_boot_args(patched)
    if args.board == "n841ap":
        apply_n841_wrapper(stock, patched)
    elif args.board == "d321ap":
        apply_d321_wrapper(stock, patched)
    elif args.board == "d79ap":
        apply_d79_wrapper(stock, patched, boot_args_slot)
    else:
        print(
            f"board {args.board}: no board-specific IMG4 wrapper "
            "(relying on ASN1-near image4 site from patchfinder)"
        )
    args.output.write_bytes(patched)
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
