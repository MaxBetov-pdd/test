#!/usr/bin/env python3
"""
Rebuild an IM4P around a patched payload, preserving everything the original carried.

`pyimg4 im4p create` drops the trailing PAYP element that shipping IM4Ps carry, so it has
to be re-appended and the outer ASN.1 length corrected. Upstream does this with hardcoded
slices ([2:5] for TXM, [2:6] for the kernelcache) and hardcoded metadata.

Two things are done differently here:

**Metadata is read from the original, not hardcoded.** Upstream repacks the b4 kernelcache
as fourcc `rkrn` with description `KernelManagement_host-511`, but Apple ships it as `krnl`
with `KernelManagement_host-514`. Those numbers came from an older build. Preserving what
the original actually declares is provably correct: repacking an UNPATCHED payload that way
reproduces Apple's file byte for byte, verified for both TXM and the kernelcache.

**The ASN.1 length width is parsed, not assumed.** The hardcoded slices happen to be right
for these two files, but they encode an assumption about DER long-form length width that
nothing enforces.

Verification after every repack:
  1. the outer ASN.1 length agrees with the real file size
  2. pyimg4 can parse the result
  3. extracting the payload back yields exactly the bytes that went in

Step 3 is the one that matters: it proves the patched payload survived the round trip.

    ./repack.py --orig <shipped.im4p> --payload <patched.raw> --out <new.im4p>
    ./repack.py --selftest        prove an unpatched repack reproduces the originals
"""

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def der_len(data):
    """-> (header_size, declared_length) for a DER element at offset 0."""
    if data[1] & 0x80 == 0:
        return 2, data[1]
    n = data[1] & 0x7F
    return 2 + n, int.from_bytes(data[2:2 + n], "big")


def set_der_len(buf, value):
    """Rewrite the outer length in place, keeping the original width."""
    n = buf[1] & 0x7F
    if not n:
        raise ValueError("outer element uses short-form length; unexpected for an IM4P")
    if value >= 1 << (8 * n):
        raise ValueError(f"length {value} does not fit in the original {n}-byte field")
    buf[2:2 + n] = value.to_bytes(n, "big")


def im4p_meta(path):
    """FourCC and description as pyimg4 reports them."""
    out = subprocess.run(["pyimg4", "im4p", "info", "-i", str(path)],
                         capture_output=True, text=True).stdout
    fourcc = re.search(r"FourCC:\s*(\S+)", out)
    desc = re.search(r"Description:\s*(.*)", out)
    return (fourcc.group(1) if fourcc else None,
            desc.group(1).strip() if desc else "")


def payp_tail(data):
    """The trailing [0]{SEQUENCE{"PAYP" ...}} element, or b'' if absent."""
    off = data.rfind(b"PAYP")
    if off < 0:
        return b""
    # "PAYP" is preceded by: a0 <len> | 30 <len> | 16 04.  Walk back over the IA5String
    # header (2 bytes) then the two nested headers, parsing each length width rather than
    # assuming the 2-byte form upstream relies on.
    start = off - 2                       # the `16 04`
    for _ in range(2):
        found = None
        for back in range(2, 8):
            cand = start - back
            if cand < 0:
                break
            hdr, ln = der_len(data[cand:cand + 8])
            if hdr == back and cand + hdr + ln == len(data):
                found = cand
                break
        if found is None:
            raise ValueError("could not parse the PAYP element header")
        start = found
    return data[start:]


def repack(orig_path, payload_path, out_path, fourcc=None, desc=None, verbose=True):
    orig = Path(orig_path).read_bytes()
    payload = Path(payload_path).read_bytes()

    f, d = im4p_meta(orig_path)
    fourcc = fourcc or f
    desc = desc if desc is not None else d
    tail = payp_tail(orig)

    if verbose:
        print(f"  source metadata : fourcc={fourcc!r} description={desc!r}")
        print(f"  PAYP tail       : {len(tail)} bytes")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "new.im4p"
        cmd = ["pyimg4", "im4p", "create", "-i", str(payload_path), "-o", str(tmp),
               "-f", fourcc, "--lzfse"]
        if desc:
            cmd += ["-d", desc]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"[err] pyimg4 im4p create failed:\n{r.stderr}")
        buf = bytearray(tmp.read_bytes())

    if tail:
        buf += tail
        hdr, ln = der_len(buf)
        set_der_len(buf, ln + len(tail))

    Path(out_path).write_bytes(buf)

    # --- verification -------------------------------------------------------------
    hdr, ln = der_len(buf)
    ok_len = hdr + ln == len(buf)
    f2, d2 = im4p_meta(out_path)
    ok_meta = (f2 == fourcc)

    with tempfile.TemporaryDirectory() as td:
        back = Path(td) / "back.raw"
        subprocess.run(["pyimg4", "im4p", "extract", "-i", str(out_path), "-o", str(back)],
                       capture_output=True, text=True)
        ok_round = back.exists() and back.read_bytes() == payload

    if verbose:
        print(f"  wrote {out_path}  ({len(buf)} bytes)")
        print(f"    ASN.1 length consistent : {ok_len}")
        print(f"    parses, fourcc preserved: {ok_meta} ({f2})")
        print(f"    payload round-trips     : {ok_round}")
    return ok_len and ok_meta and ok_round


COMPONENTS = [
    ("TXM", "ipsw/n104_24A5390f/Firmware/txm.iphoneos.release.im4p",
     "offsets/txm/txm_b4_n104.raw"),
    ("kernelcache", "ipsw/n104_24A5390f/kernelcache.release.iphone12b",
     "offsets/kc/kc_b4_n104.raw"),
]


def selftest():
    """Repacking an UNPATCHED payload must reproduce Apple's file byte for byte."""
    ok = True
    with tempfile.TemporaryDirectory() as td:
        for name, orig, raw in COMPONENTS:
            o, r = HERE / orig, HERE / raw
            out = Path(td) / f"{name}.im4p"
            print(f"\n=== {name} ===")
            good = repack(o, r, out)
            identical = out.read_bytes() == o.read_bytes()
            print(f"    byte-identical to Apple's original: {identical}")
            ok &= good and identical
    print(f"\nselftest: {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orig", help="the shipped .im4p to take metadata and PAYP from")
    ap.add_argument("--payload", help="patched raw payload")
    ap.add_argument("--out", help="output .im4p")
    ap.add_argument("--fourcc", help="override the fourcc (default: same as --orig)")
    ap.add_argument("--desc", help="override the description (default: same as --orig)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return 0 if selftest() else 1
    if not (args.orig and args.payload and args.out):
        sys.exit("[err] need --orig, --payload and --out (or --selftest)")
    return 0 if repack(args.orig, args.payload, args.out, args.fourcc, args.desc) else 1


if __name__ == "__main__":
    sys.exit(main())
