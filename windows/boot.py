#!/usr/bin/env python3
"""Boot an already-built ICH bootchain through libusb on Windows or Linux."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

from ich_usb import (
    IchUsbError,
    RecoveryClient,
    direct_pwned_dfu_boot,
    format_query,
    query_device,
    query_mode,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BOOTARGS = "rd=md0 -v debug=0x14e serial=3 wdt=-1 keepsyms=1"
FIRMWARE_NAMES = ("PMP", "AOP", "ANE", "AVE", "ISP", "GFX", "SIO")


def progress(done: int, total: int) -> None:
    pct = int(done * 100 / total) if total else 100
    print(f"\r  {done:#x} / {total:#x} ({pct:3d}%)", end="", flush=True)
    if done >= total:
        print()


def warning(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def find_bootchain(value: str | None) -> Path:
    if value:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = ROOT / candidate
        return candidate.resolve()
    marker = ROOT / ".last_bootchain"
    if marker.is_file():
        name = marker.read_text(encoding="utf-8").strip()
        if name:
            return (ROOT / "bootchain" / name).resolve()
    raise IchUsbError("No bootchain selected. Pass --bootchain PATH or build one first.")


def require_files(bootchain: Path) -> None:
    required = (
        "iBoot.patched.bin",
        "devicetree.img4",
        "trustcache.img4",
        "ramdisk.img4",
        "kernelcache.img4",
    )
    missing = [name for name in required if not (bootchain / name).is_file()]
    if missing:
        raise IchUsbError(f"Bootchain is incomplete; missing: {', '.join(missing)}")


def wait_for_recovery(timeout: float) -> None:
    print(f"Waiting for USB Recovery (up to {int(timeout)}s)...")
    deadline = time.monotonic() + timeout
    last = None
    prompted = False
    while time.monotonic() < deadline:
        try:
            mode = query_mode() or "none"
        except IchUsbError:
            mode = "none"
        if mode != last:
            print(f"  USB MODE: {mode}")
            last = mode
        if mode == "Recovery":
            try:
                with RecoveryClient(timeout=2) as client:
                    client.send_command("getenv build-version", timeout_ms=5_000)
                print("  iBoot Recovery ready")
                return
            except IchUsbError:
                pass
        elapsed = timeout - max(0.0, deadline - time.monotonic())
        if elapsed >= 25 and not prompted:
            print("  Recovery has not appeared. Unplug Lightning, wait 2 seconds, then reconnect once.")
            prompted = True
        time.sleep(1)
    raise IchUsbError("Timed out waiting for Recovery after the patched iBoot upload")


def upload(client: RecoveryClient, path: Path, command: str | None = None) -> None:
    print(f"Loading {path.name} ({path.stat().st_size} bytes)...")
    client.send_file(path, progress=progress)
    if command:
        client.send_command(command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Boot an ICH A12/A13 ramdisk through libusb")
    parser.add_argument("--bootchain", help="bootchain directory; defaults to .last_bootchain")
    parser.add_argument("--bootargs", default=DEFAULT_BOOTARGS)
    parser.add_argument(
        "--boot-command",
        choices=("bootx", "memboot"),
        default="bootx",
        help="patched iBoot command used for the final kernel handoff (default: bootx)",
    )
    parser.add_argument("--recovery-timeout", type=float, default=120)
    parser.add_argument(
        "--expected-board",
        default=os.environ.get("ICH_EXPECTED_BOARD", ""),
        help="fail unless the connected board matches (or set ICH_EXPECTED_BOARD)",
    )
    parser.add_argument(
        "--expected-ecid",
        default=os.environ.get("ICH_EXPECTED_ECID", ""),
        help="fail unless the connected ECID matches (or set ICH_EXPECTED_ECID)",
    )
    parser.add_argument(
        "--console-log",
        default=str(ROOT / "boot-console.log"),
        help="capture the iBoot USB console around the final handoff (default: boot-console.log)",
    )
    parser.add_argument(
        "--console-wait",
        type=float,
        default=20,
        help="seconds to keep capturing the USB console after the final handoff",
    )
    parser.add_argument(
        "--resume-recovery",
        action="store_true",
        help="skip the DFU/iBoot stage and continue from an already booted patched Recovery",
    )
    fw = parser.add_mutually_exclusive_group()
    fw.add_argument("--with-fw", action="store_true", help="force coprocessor firmware upload")
    fw.add_argument("--no-fw", action="store_true", help="skip coprocessor firmware upload")
    logo = parser.add_mutually_exclusive_group()
    logo.add_argument("--logo", action="store_true", help="upload bootchain/logo.img4 when present")
    logo.add_argument("--no-logo", action="store_true", help="skip logo (Windows default)")
    sep = parser.add_mutually_exclusive_group()
    sep.add_argument("--sep", action="store_true", help="force RestoreSEP upload")
    sep.add_argument("--no-sep", action="store_true", help="skip RestoreSEP")
    return parser.parse_args()


def normalized_hex(value: str) -> str:
    text = value.strip().lower().removeprefix("0x").replace("_", "")
    if not text or any(character not in "0123456789abcdef" for character in text):
        raise IchUsbError(f"Invalid hexadecimal identity value: {value!r}")
    return text.lstrip("0") or "0"


def require_expected_target(info, args: argparse.Namespace) -> None:
    if args.expected_board and info.board.casefold() != args.expected_board.casefold():
        raise IchUsbError(
            f"Connected board is {info.board or 'unknown'}, expected {args.expected_board}"
        )
    if args.expected_ecid and normalized_hex(info.ecid) != normalized_hex(args.expected_ecid):
        raise IchUsbError(
            f"Connected ECID is {info.ecid or 'unknown'}, expected {args.expected_ecid}"
        )


def main() -> int:
    args = parse_args()
    try:
        bootchain = find_bootchain(args.bootchain)
        if not bootchain.is_dir():
            raise IchUsbError(f"Bootchain directory does not exist: {bootchain}")
        require_files(bootchain)

        with_fw = args.with_fw or (not args.no_fw and (bootchain / "with-fw.enabled").is_file())
        use_logo = args.logo and not args.no_logo
        use_ibss = (bootchain / "use-ibss").is_file() and (bootchain / "iBSS.patched.bin").is_file()
        sep_file = bootchain / "sep-firmware.img4"
        use_sep = args.sep or (not args.no_sep and sep_file.is_file())

        print(f"Bootchain: {bootchain}")
        print(f"USB firmwares: {'enabled' if with_fw else 'disabled'}")

        if args.resume_recovery:
            info = query_device(timeout=8)
            mode = info.mode if info is not None else None
            if mode != "Recovery":
                raise IchUsbError(
                    f"--resume-recovery requires MODE=Recovery; got MODE={mode or 'none'}"
                )
            require_expected_target(info, args)
            print("Resuming from the already booted patched Recovery...")
            with RecoveryClient(timeout=10) as client:
                print(f"Recovery build: {client.getenv('build-version')}")
        else:
            info = query_device(timeout=8)
            if info is None:
                raise IchUsbError("No Apple DFU device found")
            print(format_query(info))
            require_expected_target(info, args)
            if info.mode != "DFU" or info.pwned.lower() != "usbliter8":
                raise IchUsbError(
                    f"Need pwned DFU (MODE=DFU, PWND=usbliter8); got MODE={info.mode}, PWND={info.pwned or 'none'}"
                )

            if use_ibss:
                print("Loading iBSS (direct pwned DFU)...")
                direct_pwned_dfu_boot(bootchain / "iBSS.patched.bin", progress=progress, warning=warning)
                time.sleep(4)
                with RecoveryClient(timeout=20) as client:
                    ibec = bootchain / "iBEC.patched.img4"
                    if not ibec.is_file():
                        ibec = bootchain / "iBoot.patched.bin"
                    upload(client, ibec)
                    try:
                        client.send_command("go")
                    except IchUsbError as exc:
                        warning(str(exc))
                time.sleep(3)
            else:
                print("Loading iBEC (direct pwned DFU)...")
                direct_pwned_dfu_boot(bootchain / "iBoot.patched.bin", progress=progress, warning=warning)
                time.sleep(5)

            wait_for_recovery(args.recovery_timeout)

        console_path = Path(args.console_log).expanduser().resolve()
        console_path.parent.mkdir(parents=True, exist_ok=True)
        console_lock = threading.Lock()
        console_file = console_path.open("wb")

        def console_output(data: bytes) -> None:
            with console_lock:
                console_file.write(data)
                console_file.flush()
                print(data.decode("utf-8", errors="replace"), end="", flush=True)

        with console_file, RecoveryClient(timeout=10) as client:
            console_started = False
            try:
                client.start_console(console_output)
                console_started = True
                print(f"Capturing iBoot console: {console_path}")
                time.sleep(1)
            except IchUsbError as exc:
                warning(f"USB console unavailable: {exc}")

            try:
                client.send_command("bgcolor 0 0 0")
            except IchUsbError as exc:
                warning(str(exc))

            logo_file = bootchain / "logo.img4"
            if use_logo:
                if logo_file.is_file():
                    try:
                        upload(client, logo_file)
                        client.send_command("setpicture 1")
                        time.sleep(3)
                    except IchUsbError as exc:
                        warning(f"logo failed: {exc}")
                else:
                    warning("--logo requested, but bootchain/logo.img4 is missing")

            for name, command in (("sptm.img4", "firmware"), ("txm.img4", "firmware")):
                path = bootchain / name
                if path.is_file():
                    upload(client, path, command)

            if use_sep:
                if not sep_file.is_file():
                    raise IchUsbError("--sep requested, but sep-firmware.img4 is missing")
                upload(client, sep_file, "rsepfirmware")

            if with_fw and not use_ibss:
                for name in FIRMWARE_NAMES:
                    path = bootchain / f"{name}.img4"
                    if path.is_file():
                        upload(client, path, "firmware")

            upload(client, bootchain / "devicetree.img4", "devicetree")
            upload(client, bootchain / "trustcache.img4", "firmware")
            upload(client, bootchain / "ramdisk.img4")
            time.sleep(2)
            client.send_command("ramdisk")

            if with_fw and use_ibss:
                for name in FIRMWARE_NAMES:
                    path = bootchain / f"{name}.img4"
                    if path.is_file():
                        upload(client, path, "firmware")

            upload(client, bootchain / "kernelcache.img4")
            print(f"Setting boot-args: {args.bootargs}")
            try:
                client.send_command(f"setenvnp boot-args {args.bootargs}")
            except IchUsbError:
                client.send_command(f"setenv boot-args {args.bootargs}")
            print(f"{args.boot_command}...")
            try:
                client.send_command(args.boot_command)
            except IchUsbError as exc:
                # USB normally disappears immediately when the handoff succeeds.
                warning(f"{args.boot_command} disconnected USB: {exc}")
            if console_started and args.console_wait > 0:
                time.sleep(args.console_wait)
            client.stop_console()

        print("Ramdisk boot was triggered.")
        if sys.platform == "win32":
            print("Next: .\\.venv\\Scripts\\python.exe .\\windows\\iproxy.py 2222 22")
        else:
            print("Next: ich iproxy 2222 22")
        print("Then: ssh root@localhost -p 2222   (password: alpine)")
        return 0
    except (IchUsbError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
