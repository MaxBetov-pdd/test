#!/usr/bin/env python3
"""
Patch restored_external and asr inside the restore ramdisk, then repack it.

Upstream does this as:

    sudo hdiutil attach -mountpoint CFW_RD ramdisk.dmg -owners off
    <patch in place>
    ldid -S -M -Cadhoc CFW_RD/usr/local/bin/restored_external
    sudo hdiutil detach -force CFW_RD
    pyimg4 im4p create -i ramdisk.dmg -o <out> -f rdsk

Three differences here, each with a reason.

**No sudo.** The sudo in upstream's line is needed for `-mountpoint <custom path>`, not for
writing. Attaching without that flag mounts at /Volumes/ramdisk and is writable as a normal
user.

**Ownership is preserved.** ldid does not modify in place, it writes a new file and renames
(verified: the inode changes). Under a non-root mount that new inode would be owned by the
invoking user rather than root. So each binary is copied out, patched and signed outside the
image, then written back into the ORIGINAL file with a plain r+b write plus truncate. That
keeps the inode, and with it whatever uid, gid and mode the file already had.

**The code-signing identifier is kept.** ldid derives the identifier from the filename, so a
plain re-sign turns `com.apple.asr` into `asr`. Passing -I keeps the original.

Entitlements are preserved by ldid's -M in all cases; verified that restored_external keeps
all 10870 bytes of them and asr keeps its 310.

    ./ramdisk_patch.py --dmg ramdisk.dmg            dry run
    ./ramdisk_patch.py --dmg ramdisk.dmg --apply
    ./ramdisk_patch.py --dmg ramdisk.dmg --apply --repack-to 094-13753-162.dmg
"""

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools"
LDID = TOOLS / "ldid_macosx_arm64"
APPLY = HERE / "apply_patches.py"

# (path inside the ramdisk, patch table, expected code-signing identifier)
TARGETS = [
    ("usr/local/bin/restored_external", "restored_external", "com.apple.restored_external"),
    ("usr/sbin/asr", "asr", "com.apple.asr"),
]


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def attach(dmg):
    """Attach read-write at the default mount point. No sudo needed."""
    r = run(["hdiutil", "attach", "-nobrowse", str(dmg)])
    if r.returncode != 0:
        sys.exit(f"[err] attach failed: {r.stderr.strip()}")
    dev = next((l.split()[0] for l in r.stdout.splitlines() if l.startswith("/dev/")), None)
    mnt = next((re.search(r"(/Volumes/\S.*)$", l).group(1)
                for l in r.stdout.splitlines() if "/Volumes/" in l), None)
    if not dev or not mnt:
        sys.exit(f"[err] could not parse attach output:\n{r.stdout}")
    return dev, Path(mnt.strip())


def detach(dev):
    r = run(["hdiutil", "detach", dev])
    if r.returncode != 0:
        r = run(["hdiutil", "detach", "-force", dev])
    if r.returncode != 0:
        raise RuntimeError(f"could not detach {dev}: {r.stderr.strip()}")


def identifier_of(path):
    r = run(["codesign", "-dv", str(path)])
    m = re.search(r"^Identifier=(.*)$", r.stderr, re.M)
    return m.group(1) if m else None


def ent_size(path):
    r = run([str(LDID), "-e", str(path)])
    if r.returncode != 0:
        raise RuntimeError(f"ldid could not read entitlements from {path}: {r.stderr.strip()}")
    return len(r.stdout)


def process(target, table, want_id, mnt, apply_changes):
    src = mnt / target
    if not src.exists():
        print(f"    MISSING {target}")
        return False
    before = (src.stat().st_ino, src.stat().st_uid, src.stat().st_mode,
              src.stat().st_size, ent_size(src), identifier_of(src))
    print(f"    {target}")
    print(f"      before: inode {before[0]} uid {before[1]} mode {before[2] & 0o7777:o} "
          f"size {before[3]} ents {before[4]}B id {before[5]}")

    with tempfile.TemporaryDirectory() as td:
        # Sign outside the image, under the ORIGINAL basename so ldid's default
        # identifier is sane even if -I were ever dropped.
        work = Path(td) / Path(target).name
        shutil.copy2(src, work)

        r = run([sys.executable, str(APPLY), table, str(work), "-o", str(work)])
        if r.returncode != 0 or "[+]" not in r.stdout:
            print(f"      PATCH FAILED:\n{r.stdout}{r.stderr}")
            return False
        print(f"      patched: {r.stdout.strip().splitlines()[-2].strip()}")

        r = run([str(LDID), f"-I{want_id}", "-S", "-M", "-Cadhoc", str(work)])
        if r.returncode != 0:
            print(f"      SIGN FAILED: {r.stderr.strip()}")
            return False

        signed = work.read_bytes()
        got_id, got_ents = identifier_of(work), ent_size(work)
        print(f"      signed : size {len(signed)} ents {got_ents}B id {got_id}")

        if got_ents != before[4]:
            print(f"      !! entitlements changed ({before[4]} -> {got_ents})")
            return False
        if got_id != want_id:
            print(f"      !! identifier is {got_id}, expected {want_id}")
            return False

        if not apply_changes:
            print("      [dry-run] not written back")
            return True

        # In-place content replacement: keeps the inode, so uid/gid/mode survive.
        with open(src, "r+b") as f:
            f.write(signed)
            f.truncate(len(signed))

    st = src.stat()
    ok = st.st_ino == before[0] and st.st_uid == before[1] and st.st_mode == before[2]
    print(f"      after : inode {st.st_ino} uid {st.st_uid} mode {st.st_mode & 0o7777:o} "
          f"size {st.st_size}   inode/uid/mode preserved: {ok}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dmg", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--repack-to", help="write a new rdsk .im4p here (needs --apply)")
    ap.add_argument("--orig-im4p", help="shipped ramdisk .im4p, for metadata on repack")
    args = ap.parse_args()

    dmg = Path(args.dmg)
    print(f"[*] attaching {dmg.name} read-write (no sudo)")
    dev, mnt = attach(dmg)
    print(f"    {dev} at {mnt}")
    ok = True
    try:
        for target, table, want_id in TARGETS:
            ok &= process(target, table, want_id, mnt, args.apply)
    finally:
        detach(dev)
        print(f"[*] detached {dev}")

    if not ok:
        sys.exit("\n[err] one or more targets failed; ramdisk may be inconsistent")
    if not args.apply:
        print("\n[dry-run] nothing written. re-run with --apply")
        return 0

    if args.repack_to:
        cmd = ["pyimg4", "im4p", "create", "-i", str(dmg), "-o", args.repack_to, "-f", "rdsk"]
        if args.orig_im4p:
            desc = re.search(r"Description:\s*(.*)",
                             run(["pyimg4", "im4p", "info", "-i", args.orig_im4p]).stdout)
            if desc and desc.group(1).strip():
                cmd += ["-d", desc.group(1).strip()]
        r = run(cmd)
        if r.returncode != 0:
            sys.exit(f"[err] repack failed: {r.stderr}")
        print(f"[+] repacked to {args.repack_to} ({Path(args.repack_to).stat().st_size} bytes)")
    print("\n[+] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
