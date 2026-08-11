#!/usr/bin/env python3
"""Reproduce the offline d79 firmware-port verification.

The input is a selectively extracted iPhone12,8 / 24A5390f IPSW tree created by
get_fw.py. Payloads are decompressed into a temporary directory; the source tree is
never modified.
"""

import argparse
import hashlib
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import patch_dt
import patch_dt2
from apply_patches import TABLES
from get_fw import BOARD, BUILD, DEVICE, EXTRACT_DIR, validate_manifest


HERE = Path(__file__).resolve().parent
EXPECTED_DT_SHA256 = "9148820fc1e4d12f5f344eaea504d4b750b9f2d4489a92ce5717896bbade9c5a"

COMPONENTS = {
    "iBSS": "iBSS",
    "iBEC": "iBEC",
    "TXM": "Ap,RestoreTrustedExecutionMonitor",
    "kernel": "RestoreKernelCache",
    "DeviceTree": "RestoreDeviceTree",
}

PATCH_MATRIX = {
    "iBSS": ("ibss-restore", "ibss-ramdisk", "ibss-normal", "ibss-normal-diag"),
    "iBEC": ("ibss-restore", "ibss-ramdisk", "ibss-normal", "ibss-normal-diag"),
    "TXM": ("txm-restore", "txm-boot"),
    "kernel": ("kc-restore", "kc-boot", "kc-diag"),
}


class VerificationError(RuntimeError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def member_path(tree, member):
    parts = PurePosixPath(member).parts
    if member.startswith("/") or ".." in parts:
        raise VerificationError(f"unsafe component path in manifest: {member!r}")
    path = tree.joinpath(*parts)
    if not path.is_file():
        raise VerificationError(f"component is missing from extracted IPSW: {member}")
    return path


def load_identity(tree):
    manifest_path = tree / "BuildManifest.plist"
    if not manifest_path.is_file():
        raise VerificationError(
            f"missing {manifest_path}; run ./get_fw.py before this verifier")
    with manifest_path.open("rb") as stream:
        manifest = plistlib.load(stream)
    try:
        identity = validate_manifest(manifest)
    except SystemExit as exc:
        raise VerificationError(str(exc)) from exc
    print(f"[OK] manifest: {DEVICE} / {BOARD} / {BUILD}")
    return identity


def resolve_components(tree, identity):
    manifest = identity.get("Manifest", {})
    resolved = {}
    for label, key in COMPONENTS.items():
        try:
            member = manifest[key]["Info"]["Path"]
        except KeyError as exc:
            raise VerificationError(f"manifest has no {key!r} component path") from exc
        resolved[label] = member_path(tree, member)
    return resolved


def extract_payload(pyimg4, source, output):
    proc = subprocess.run(
        [pyimg4, "im4p", "extract", "-i", str(source), "-o", str(output)],
        text=True,
        capture_output=True,
    )
    if proc.returncode:
        detail = (proc.stderr or proc.stdout).strip()
        raise VerificationError(
            f"pyimg4 could not extract {source.name}:\n{detail}")
    if not output.is_file() or output.stat().st_size == 0:
        raise VerificationError(f"pyimg4 produced no payload for {source.name}")
    return output.read_bytes()


def verify_patch_table(table_name, label, data):
    (target_name, target_size, target_sha), patches = TABLES[table_name]
    got_sha = sha256(data)
    if len(data) != target_size:
        raise VerificationError(
            f"{label}/{table_name}: {len(data)} bytes, expected {target_size} "
            f"for {target_name}")
    if got_sha != target_sha:
        raise VerificationError(
            f"{label}/{table_name}: SHA-256 {got_sha}, expected pristine {target_sha}")

    failures = []
    for item in patches:
        state, detail = item.check(data)
        if state is not True:
            failures.append(f"{item.off:#x}: {detail}")
    if failures:
        raise VerificationError(
            f"{label}/{table_name}: {len(failures)} guarded offsets failed:\n  "
            + "\n  ".join(failures))
    print(f"[OK] {label:10s} {table_name:14s} {len(patches):3d} patches")


def verify_dt_state(module, data):
    tree = module.DeviceTree.parse(data)
    for operation, node_path, name, value in module.PATCHES:
        node = tree.node(node_path)
        prop = node.prop(name) if node else None
        good = (
            prop is None
            if operation == "del-prop"
            else prop is not None and prop.value == value
        )
        if not good:
            return False
    return True


def verify_devicetree(raw):
    got_sha = sha256(raw)
    if got_sha != EXPECTED_DT_SHA256:
        raise VerificationError(
            f"DeviceTree SHA-256 {got_sha}, expected pristine {EXPECTED_DT_SHA256}")

    for label, module in (("restore/SSHRD", patch_dt), ("normal boot", patch_dt2)):
        tree = module.DeviceTree.parse(raw)
        if tree.serialize() != raw:
            raise VerificationError(f"DeviceTree {label}: pristine lossless round-trip failed")
        report = module.apply_patches(tree)
        output = tree.serialize()
        if not verify_dt_state(module, output):
            raise VerificationError(f"DeviceTree {label}: structural post-check failed")
        changes = ", ".join(f"{target}={state}" for _, target, state in report)
        print(f"[OK] DeviceTree {label:12s} {changes}")


def find_pyimg4(explicit):
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise VerificationError(f"pyimg4 executable not found: {path}")
        return str(path.resolve())
    found = shutil.which("pyimg4")
    if not found:
        raise VerificationError(
            "pyimg4 is not on PATH; install it with `python3 -m pip install pyimg4` "
            "or pass --pyimg4 /path/to/pyimg4")
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tree",
        type=Path,
        default=HERE / EXTRACT_DIR,
        help=f"selectively extracted IPSW tree (default: {EXTRACT_DIR})",
    )
    parser.add_argument("--pyimg4", help="path to the pyimg4 executable")
    args = parser.parse_args()

    try:
        tree = args.tree.expanduser().resolve()
        identity = load_identity(tree)
        components = resolve_components(tree, identity)
        pyimg4 = find_pyimg4(args.pyimg4)

        with tempfile.TemporaryDirectory(prefix="verify-d79-") as temp_name:
            temp = Path(temp_name)
            raw = {
                label: extract_payload(pyimg4, source, temp / f"{label}.raw")
                for label, source in components.items()
            }

            for label, tables in PATCH_MATRIX.items():
                for table_name in tables:
                    verify_patch_table(table_name, label, raw[label])
            verify_devicetree(raw["DeviceTree"])

    except (OSError, plistlib.InvalidFileException, VerificationError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    print("\n[PASS] d79 offline port verification completed")
    print("       13 guarded table/binary checks + 2 DeviceTree transformations")
    print("       No phone was accessed and no source file was modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
