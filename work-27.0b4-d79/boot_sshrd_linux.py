#!/usr/bin/env python3
"""Boot the verified d79 SSH ramdisk on Linux with transition-aware logging."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


APPLE_VID = 0x05AC
DFU_PID = 0x1227
RECOVERY_PID = 0x1281
USBMUX_PID = 0x12A8
EXPECTED_PRODUCT = "iPhone12,8"
EXPECTED_BOARD = "d79ap"
EXPECTED_BUILD = "24A5390f"
EXPECTED_CPID = "8030"
EXPECTED_BDID = "10"


class BootError(RuntimeError):
    pass


def usb_modules():
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise BootError("PyUSB is missing; install the python-pyusb package") from exc
    return usb.core, usb.util


def parse_fields(text):
    return dict(re.findall(r"^([A-Z][A-Z0-9-]*):\s*(.+?)\s*$", text, re.MULTILINE))


def normalize_hex(value):
    return value.strip().lower().removeprefix("0x").lstrip("0") or "0"


def usb_token(pid):
    usb_core, usb_util = usb_modules()
    device = usb_core.find(idVendor=APPLE_VID, idProduct=pid)
    if device is None:
        return None
    token = (getattr(device, "bus", None), getattr(device, "address", None))
    usb_util.dispose_resources(device)
    return token


def query_dfu(expected_ecid):
    usb_core, usb_util = usb_modules()
    device = usb_core.find(idVendor=APPLE_VID, idProduct=DFU_PID)
    if device is None:
        raise BootError("Apple DFU device 05ac:1227 is not visible")
    try:
        serial = usb_util.get_string(device, device.iSerialNumber) or ""
    finally:
        usb_util.dispose_resources(device)
    fields = dict(
        (match.group(1), match.group(2) if match.group(2) is not None else match.group(3))
        for match in re.finditer(
            r"(?:^|\s)([A-Z0-9-]{3,16}):(?:\[([^\]]*)\]|([^\s]+))", serial
        )
    )
    if normalize_hex(fields.get("CPID", "")) != normalize_hex(EXPECTED_CPID):
        raise BootError(f"expected CPID 0x{EXPECTED_CPID}, got {fields.get('CPID', 'unknown')}")
    if normalize_hex(fields.get("BDID", "")) != normalize_hex(EXPECTED_BDID):
        raise BootError(f"expected d79 BDID 0x{EXPECTED_BDID}, got {fields.get('BDID', 'unknown')}")
    if fields.get("PWND", "").lower() != "usbliter8":
        raise BootError(f"need usbliter8 PWN DFU, got PWND={fields.get('PWND', 'none')}")
    if expected_ecid and normalize_hex(fields.get("ECID", "")) != normalize_hex(expected_ecid):
        raise BootError(f"expected ECID {expected_ecid}, got {fields.get('ECID', 'unknown')}")
    print("MODE: DFU")
    print(f"PRODUCT: {EXPECTED_PRODUCT}")
    print(f"MODEL: {EXPECTED_BOARD}")
    print(f"CPID: 0x{EXPECTED_CPID}")
    print(f"ECID: {fields.get('ECID', 'unknown')}")
    print("PWND: usbliter8")


def direct_dfu_boot(image):
    usb_core, usb_util = usb_modules()
    data = image.read_bytes()
    if not data:
        raise BootError(f"empty iBSS payload: {image}")
    device = usb_core.find(idVendor=APPLE_VID, idProduct=DFU_PID)
    if device is None:
        raise BootError("DFU device disappeared before iBSS upload")
    try:
        try:
            device.set_configuration()
        except usb_core.USBError:
            device.get_active_configuration()
        try:
            if device.is_kernel_driver_active(0):
                device.detach_kernel_driver(0)
        except (NotImplementedError, usb_core.USBError):
            pass
        usb_util.claim_interface(device, 0)
        total = len(data)
        for offset in range(0, total, 0x800):
            chunk = data[offset:offset + 0x800]
            sent = int(device.ctrl_transfer(0x21, 1, 0, 0, chunk, timeout=5000))
            if sent != len(chunk):
                raise BootError(f"short DFU upload at {offset:#x}: {sent}/{len(chunk)}")
            done = min(offset + len(chunk), total)
            print(f"\r  {done:#x} / {total:#x} ({done * 100 // total:3d}%)", end="", flush=True)
        print()
        # This is the sequence used by the working usbliter8 transport. The final
        # requests intentionally make the USB handle disappear, so their errors are
        # expected and ignored after the full payload has been acknowledged.
        for request in (1, 8, 6):
            try:
                device.ctrl_transfer(0x21, request, 0, 0, b"", timeout=1000)
            except usb_core.USBError:
                pass
    except usb_core.USBError as exc:
        raise BootError(f"DFU upload failed: {exc}") from exc
    finally:
        try:
            usb_util.release_interface(device, 0)
        except Exception:
            pass
        usb_util.dispose_resources(device)


class Recovery:
    def __init__(self, executable, board, ecid):
        self.executable = executable
        self.board = board
        self.ecid = ecid

    def run(self, *args, check=True, capture=False):
        return subprocess.run(
            [self.executable, *map(str, args)],
            check=check,
            text=True,
            capture_output=capture,
        )

    def query(self):
        result = self.run("-q", capture=True)
        return (result.stdout or "") + (result.stderr or "")

    def validate(self, text):
        fields = parse_fields(text)
        if fields.get("MODE") != "Recovery":
            raise BootError(f"expected Recovery, got {fields.get('MODE', 'unknown')}")
        if fields.get("MODEL") and fields["MODEL"] != self.board:
            raise BootError(f"expected {self.board}, got {fields['MODEL']}")
        if self.ecid and fields.get("ECID"):
            if normalize_hex(fields["ECID"]) != normalize_hex(self.ecid):
                raise BootError(f"expected ECID {self.ecid}, got {fields['ECID']}")

    def wait(self, timeout, previous=None):
        print(f"Waiting for a new native Recovery transport (up to {int(timeout)}s)...")
        deadline = time.monotonic() + timeout
        last = object()
        while time.monotonic() < deadline:
            token = usb_token(RECOVERY_PID)
            if token != last:
                print(f"  USB 05ac:1281 token: {token or 'none'}")
                last = token
            if token is not None and (previous is None or token != previous):
                try:
                    text = self.query()
                    self.validate(text)
                    print(text.rstrip())
                    print("  Native Recovery transport ready")
                    return token
                except (BootError, subprocess.CalledProcessError):
                    pass
            time.sleep(0.5)
        raise BootError("timed out waiting for a new usable Recovery transport")

    def upload(self, path):
        print(f"Loading {path.name} ({path.stat().st_size} bytes)...", flush=True)
        self.run("-f", path)

    def command(self, command, check=True):
        print(f"[irecovery] command: {command}", flush=True)
        return self.run("-c", command, check=check)


def require_file(directory, *names):
    for name in names:
        path = directory / name
        if path.is_file() and path.stat().st_size:
            return path
    raise BootError(f"artifact is missing: {' or '.join(names)}")


def file_sha256(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_artifact(directory, expected_mode):
    metadata = directory / "artifact-info.json"
    if not metadata.is_file():
        raise BootError("artifact-info.json is missing; use a newly built d79 artifact")
    try:
        info = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BootError(f"invalid artifact-info.json: {exc}") from exc
    expected = {
        "schema": 1,
        "product": EXPECTED_PRODUCT,
        "board": EXPECTED_BOARD,
        "build": EXPECTED_BUILD,
        "mode": expected_mode,
    }
    for key, value in expected.items():
        if info.get(key) != value:
            raise BootError(
                f"artifact metadata {key}={info.get(key)!r}, expected {value!r}")
    hashes = info.get("files")
    if not isinstance(hashes, dict) or not hashes:
        raise BootError("artifact metadata has no file hashes")
    for relative, wanted in hashes.items():
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise BootError(f"unsafe artifact hash path: {relative!r}")
        path = directory / relative_path
        if not path.is_file():
            raise BootError(f"artifact hash entry is missing: {relative}")
        got = file_sha256(path)
        if got != wanted:
            raise BootError(f"artifact SHA-256 mismatch: {relative}")
    print(
        f"Artifact verified: {EXPECTED_PRODUCT} / {EXPECTED_BOARD} / "
        f"{EXPECTED_BUILD} / {expected_mode} ({len(hashes)} files)"
    )


def wait_for_usbmux(timeout):
    print(f"Waiting up to {int(timeout)}s for ramdisk USB 05ac:12a8...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if usb_token(USBMUX_PID) is not None:
            print("Ramdisk USB is ready: 05ac:12a8")
            return
        time.sleep(1)
    raise BootError("ramdisk kernel was sent, but USB 05ac:12a8 did not appear")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootchain", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--expected-ecid", help="optional ECID safety lock")
    parser.add_argument("--irecovery", default=os.environ.get("IRECOVERY", "irecovery"))
    parser.add_argument("--recovery-timeout", type=float, default=600)
    parser.add_argument("--ramdisk-timeout", type=float, default=180)
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise BootError("run as root (the shell wrapper uses sudo)")
    executable = shutil.which(args.irecovery) if os.sep not in args.irecovery else args.irecovery
    if not executable or not Path(executable).is_file():
        raise BootError("irecovery was not found; install libirecovery or pass --irecovery")

    directory = args.bootchain.expanduser().resolve()
    validate_artifact(directory, "sshrd")
    files = {
        "iBSS": require_file(directory, "iBSS.raw", "iBSS.patched.bin"),
        "iBEC": require_file(directory, "iBEC.img4", "iBEC.patched.img4"),
        "logo": require_file(directory, "RestoreLogo.img4", "logo.img4"),
        "ANE": require_file(directory, "ANE.img4"),
        "AOP": require_file(directory, "AOP.img4"),
        "AVE": require_file(directory, "AVE.img4"),
        "SPTM": require_file(directory, "SPTM.img4"),
        "TXM": require_file(directory, "TXM.img4"),
        "GFX": require_file(directory, "GFX.img4"),
        "ISP": require_file(directory, "ISP.img4"),
        "PMP": require_file(directory, "PMP.img4"),
        "trustcache": require_file(directory, "RestoreTrustCache.img4", "trustcache.img4"),
        "SIO": require_file(directory, "SIO.img4"),
        "WCH": require_file(directory, "WCH.img4"),
        "ramdisk": require_file(directory, "RestoreRamdisk.img4", "ramdisk.img4"),
        "DeviceTree": require_file(directory, "DeviceTree.img4", "devicetree.img4"),
        "SEP": require_file(directory, "SEP.img4", "sep-firmware.img4"),
        "kernel": require_file(directory, "Kernelcache.img4", "kernelcache.img4"),
    }

    query_dfu(args.expected_ecid)
    recovery = Recovery(str(executable), EXPECTED_BOARD, args.expected_ecid)

    print("Loading iBSS through the usbliter8 DFU transport...")
    direct_dfu_boot(files["iBSS"])
    recovery.wait(args.recovery_timeout)

    recovery.upload(files["iBEC"])
    previous = usb_token(RECOVERY_PID)
    recovery.command("go")
    recovery.wait(args.recovery_timeout, previous)
    time.sleep(2)

    recovery.upload(files["logo"])
    recovery.command("setpicture 0x1")
    recovery.command("bgcolor 0 191 255")

    for name in ("ANE", "AOP", "AVE"):
        recovery.upload(files[name])
        recovery.command("firmware")
    time.sleep(1)
    recovery.upload(files["SPTM"])
    recovery.command("firmware")
    time.sleep(3)
    for name in ("TXM", "GFX", "ISP"):
        recovery.upload(files[name])
        recovery.command("firmware")
    time.sleep(1)
    for name in ("PMP", "trustcache", "SIO", "WCH"):
        recovery.upload(files[name])
        recovery.command("firmware")

    recovery.upload(files["ramdisk"])
    recovery.command("getenv ramdisk-delay")
    recovery.command("ramdisk")
    time.sleep(2)

    recovery.upload(files["DeviceTree"])
    recovery.command("devicetree")
    recovery.upload(files["SEP"])
    recovery.command("rsepfirmware")
    recovery.upload(files["kernel"])
    recovery.command("bootx", check=False)
    print("bootx sent; waiting for ramdisk USB is the next stage")
    wait_for_usbmux(args.ramdisk_timeout)
    print("Next: iproxy 2222 22")
    print("Then: ssh root@localhost -p 2222   (password: alpine)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BootError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
