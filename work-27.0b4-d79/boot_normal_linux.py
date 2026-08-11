#!/usr/bin/env python3
"""Run one non-restore d79 normal-boot experiment on Linux."""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from boot_sshrd_linux import (
    BootError,
    EXPECTED_BOARD,
    RECOVERY_PID,
    Recovery,
    USBMUX_PID,
    direct_dfu_boot,
    query_dfu,
    require_file,
    usb_token,
    validate_artifact,
)


def observe_post_boot(timeout, old_recovery, diagnostic=False):
    if diagnostic:
        print(
            f"Observing USB for {int(timeout)}s. Keep the phone screen visible and "
            "photograph the final panic/verbose lines."
        )
    else:
        print(f"Waiting up to {int(timeout)}s for normal-mode USB 05ac:12a8...")
    deadline = time.monotonic() + timeout
    last_normal = last_recovery = object()
    recovery_disappeared = False
    while time.monotonic() < deadline:
        normal = usb_token(USBMUX_PID)
        recovery = usb_token(RECOVERY_PID)
        if normal != last_normal or recovery != last_recovery:
            print(
                f"  normal={normal or 'none'} recovery={recovery or 'none'}",
                flush=True,
            )
            last_normal, last_recovery = normal, recovery
        if normal is not None:
            print("Normal-mode USB appeared: 05ac:12a8")
            return
        if recovery is None:
            recovery_disappeared = True
        elif recovery_disappeared and recovery != old_recovery:
            raise BootError("device left bootx and returned to a new Recovery transport")
        time.sleep(1)
    if diagnostic:
        print(
            "Diagnostic observation window ended with no USB. This is an expected "
            "diagnostic outcome; the phone screen is the kernel log source."
        )
        return
    raise BootError("normal boot was sent, but 05ac:12a8 did not appear before timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bootchain", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--expected-ecid", help="optional ECID safety lock")
    parser.add_argument("--irecovery", default=os.environ.get("IRECOVERY", "irecovery"))
    parser.add_argument("--recovery-timeout", type=float, default=600)
    parser.add_argument("--normal-timeout", type=float, default=300)
    parser.add_argument(
        "--artifact-mode",
        choices=("normal-experimental", "normal-diagnostic"),
        default="normal-experimental",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise BootError("run as root (the shell wrapper uses sudo)")
    executable = shutil.which(args.irecovery) if os.sep not in args.irecovery else args.irecovery
    if not executable or not Path(executable).is_file():
        raise BootError("irecovery was not found; install libirecovery or pass --irecovery")

    directory = args.bootchain.expanduser().resolve()
    diagnostic = args.artifact_mode == "normal-diagnostic"
    validate_artifact(directory, args.artifact_mode)
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
        "DeviceTree": require_file(directory, "DeviceTree.img4", "devicetree.img4"),
        "SEP": require_file(directory, "SEP.img4", "sep-firmware.img4"),
        "kernel": require_file(directory, "Kernelcache.img4", "kernelcache.img4"),
    }

    if diagnostic:
        print("DIAGNOSTIC: panic-preserving tethered boot; expected to stop before userland")
    else:
        print("EXPERIMENT: tethered normal boot; no restore and no host-side filesystem write")
    print("SAFETY: no ramdisk, restore command, mount, or host-side filesystem write")
    query_dfu(args.expected_ecid)
    recovery = Recovery(str(executable), EXPECTED_BOARD, args.expected_ecid)

    print("Loading normal-boot iBSS through the usbliter8 DFU transport...")
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

    recovery.upload(files["DeviceTree"])
    recovery.command("devicetree")
    recovery.upload(files["SEP"])
    recovery.command("rsepfirmware")
    recovery.upload(files["kernel"])
    old_recovery = usb_token(RECOVERY_PID)
    recovery.command("bootx", check=False)
    print("bootx sent; no ramdisk was loaded and no restore command was issued")
    observe_post_boot(args.normal_timeout, old_recovery, diagnostic)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BootError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
