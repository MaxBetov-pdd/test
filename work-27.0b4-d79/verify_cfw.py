#!/usr/bin/env python3
"""
Verify a built CFW directory before it is flashed to a device.

apply_patches.py proves each patch landed in the intermediate file. This proves the
patched bytes actually survived into the components the restore will consume: the IM4Ps
are re-parsed, their payloads decompressed, and every patch re-checked in place.

That closes the gap between "the patch was applied" and "the patch is in the firmware",
which spans repacking, PAYP re-append, ASN.1 length fixup, and for the ramdisk a mount,
a re-sign and a rebuild.

Each patch must report as ALREADY APPLIED. A patch reporting as still-pending means that
component was rebuilt from a pristine payload somewhere and the patch was lost.

    ./verify_cfw.py
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import apply_patches as ap  # noqa: E402

# (label, path relative to the work dir, patch table, kind)
COMPONENTS = [
    ("iBSS (raw, fed to usbliter8ctl)", "Ramdisk/iBSS.raw", "ibss-restore", "raw"),
    ("iBEC", "CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p", "ibss-restore", "im4p"),
    ("TXM", "CFW/Firmware/txm.iphoneos.release.im4p", "txm-restore", "im4p"),
    ("kernelcache", "CFW/kernelcache.release.iphone12c", "kc-restore", "im4p"),
]

# Inside the repacked restore ramdisk.
RAMDISK = "CFW/094-13753-162.dmg"
RAMDISK_BINARIES = [
    ("usr/local/bin/restored_external", "restored_external"),
    ("usr/sbin/asr", "asr"),
]

DEVICETREE = "CFW/Firmware/all_flash/DeviceTree.d79ap.im4p"


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def extract_payload(path, out):
    """Decompress an IM4P payload. Returns True on success."""
    r = run(["pyimg4", "im4p", "extract", "-i", str(path), "-o", str(out)])
    return out.exists() and out.stat().st_size > 0


def check_patches(data, table, skip_identity=False):
    """-> (applied, pending, failed) counts for a table against these bytes."""
    ident, patches = ap.TABLES[table]
    if not skip_identity and len(data) != ident[1]:
        return None, None, f"size {len(data)} != expected {ident[1]}"
    applied = pending = failed = 0
    for p in patches:
        ok, _ = p.check(data)
        if ok is None:
            applied += 1          # already patched, which is what we want here
        elif ok is True:
            pending += 1          # still pristine: the patch is NOT in this file
        else:
            failed += 1           # neither original nor patched value
    return applied, pending, failed


class Report:
    def __init__(self):
        self.problems = []

    def line(self, ok, label, detail):
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<38} {detail}")
        if not ok:
            self.problems.append(label)


def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--dir", default=".")
    args = ap_.parse_args()
    work = (HERE / args.dir) if not Path(args.dir).is_absolute() else Path(args.dir)
    if not work.is_dir():
        sys.exit(f"[err] no such directory: {work}")

    print(f"[*] verifying built CFW in {work}\n")
    r = Report()

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        for label, rel, table, kind in COMPONENTS:
            p = work / rel
            if not p.exists():
                r.line(False, label, "MISSING")
                continue
            if kind == "raw":
                data = p.read_bytes()
            else:
                out = td / (p.name + ".payload")
                if not extract_payload(p, out):
                    r.line(False, label, "could not extract payload")
                    continue
                data = out.read_bytes()
            applied, pending, failed = check_patches(data, table)
            if applied is None:
                r.line(False, label, failed)
                continue
            total = applied + pending + failed
            r.line(pending == 0 and failed == 0, label,
                   f"{applied}/{total} patches present"
                   + (f", {pending} MISSING" if pending else "")
                   + (f", {failed} corrupt" if failed else ""))

        # DeviceTree: the patch removes a property, so this has to be a structural check.
        # A substring search for b"content-protect" gives a false positive: the tree also
        # contains "disable-av-content-protection", an unrelated property that contains it.
        dt = work / DEVICETREE
        if not dt.exists():
            r.line(False, "DeviceTree", "MISSING")
        else:
            out = td / "dt.raw"
            if not extract_payload(dt, out):
                r.line(False, "DeviceTree", "could not extract payload")
            else:
                sys.path.insert(0, str(work))
                try:
                    from patch_dt import DeviceTree
                    node = DeviceTree.parse(out.read_bytes()).node("/defaults")
                    if node is None:
                        r.line(False, "DeviceTree", "/defaults node not found")
                    else:
                        present = node.prop("content-protect") is not None
                        r.line(not present, "DeviceTree",
                               "/defaults/content-protect removed" if not present
                               else "/defaults/content-protect STILL PRESENT")
                except Exception as e:
                    r.line(False, "DeviceTree", f"{type(e).__name__}: {e}")

        # Ramdisk: mount the repacked image read-only and check the two binaries.
        rd = work / RAMDISK
        if not rd.exists():
            r.line(False, "restore ramdisk", "MISSING")
        else:
            dmg = td / "rd.dmg"
            if not extract_payload(rd, dmg):
                r.line(False, "restore ramdisk", "could not extract payload")
            else:
                att = run(["hdiutil", "attach", "-readonly", "-nobrowse", str(dmg)])
                mnt = next((l.split("\t")[-1].strip() for l in att.stdout.splitlines()
                            if "/Volumes/" in l), None)
                if not mnt:
                    r.line(False, "restore ramdisk", "could not mount")
                else:
                    dev = next(l.split()[0] for l in att.stdout.splitlines()
                               if l.startswith("/dev/"))
                    try:
                        for rel, table in RAMDISK_BINARIES:
                            f = Path(mnt) / rel
                            if not f.exists():
                                r.line(False, f"ramdisk: {Path(rel).name}", "MISSING")
                                continue
                            # Skip the identity gate: these are re-signed, so smaller
                            # than the pristine binary the table's size refers to.
                            a, pend, fail = check_patches(f.read_bytes(), table,
                                                          skip_identity=True)
                            r.line(pend == 0 and fail == 0, f"ramdisk: {Path(rel).name}",
                                   f"{a}/{a + pend + fail} patches present"
                                   + (f", {pend} MISSING" if pend else "")
                                   + (f", {fail} corrupt" if fail else ""))
                    finally:
                        run(["hdiutil", "detach", dev])

    print()
    if r.problems:
        print(f"[!] {len(r.problems)} problem(s): {', '.join(r.problems)}")
        print("[!] DO NOT FLASH THIS BUILD")
        return 1
    print("[+] CFW integrity verified: every patch is present in the built components")
    return 0


if __name__ == "__main__":
    sys.exit(main())
