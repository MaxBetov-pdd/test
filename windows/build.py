#!/usr/bin/env python3
"""Windows host builder for ICH A12/A13 bootchains.

All firmware download, IMG4, patching and trust-cache work is native Python.
The one operation Windows cannot yet perform reliably is expanding Apple's APFS
RestoreRamDisk.  Supply a raw, already-expanded/injected image with
``--prepared-ramdisk``; without it this command exports the stock image and an
explicit preparation job instead of silently producing a ramdisk without SSH.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import urllib.parse
import zipfile
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import requests
from remotezip import RemoteZip

from ich_usb import IchUsbError, query_device
from img4tools import (
    Img4ToolError,
    extract_file,
    extract_im4p,
    extract_raw_or_img4,
    wrap_existing_im4p,
    wrap_kernel,
    wrap_raw,
)
from trustcache import TrustCacheError, build_trustcache


ROOT = Path(__file__).resolve().parent.parent
PATCH = ROOT / "patch"
RESOURCES = ROOT / "resources"
CACHE_ROOT = ROOT / "cache"
WORK_ROOT = ROOT / "work"
BOOTCHAIN_ROOT = ROOT / "bootchain"
PREPARED_ROOT = ROOT / "prepared-ramdisk"
MAX_RAMDISK_BYTES = 280 * 1024 * 1024
MIN_PREPARED_RAMDISK_BYTES = 256 * 1024 * 1024

REQUIRED_COMPONENTS = {
    "iBEC": ("iBEC",),
    "DeviceTree": ("DeviceTree",),
    "KernelCache": ("KernelCache",),
    "RestoreRamDisk": ("RestoreRamDisk",),
    "RestoreTrustCache": ("RestoreTrustCache",),
}
OPTIONAL_COMPONENTS = {
    "iBSS": ("iBSS",),
    "RestoreSEP": ("RestoreSEP",),
    "SPTM": ("SPTM", "Ap,SPTM", "SecurePageTableMonitor", "RestoreSPTM"),
    "TXM": ("TXM", "Ap,TXM", "TrustedExecutionMonitor", "Ap,TrustedExecutionMonitor"),
    "AOP": ("AOP",),
    "ANE": ("ANE",),
    "AVE": ("AVE",),
    "ISP": ("ISP",),
    "GFX": ("GFX",),
    "SIO": ("SIO",),
    "PMP": ("PMP", "Ap,PMP"),
}
FIRMWARE_COMPONENTS = ("PMP", "AOP", "ANE", "AVE", "ISP", "GFX", "SIO")


class BuildError(RuntimeError):
    pass


class RamdiskNeeded(BuildError):
    def __init__(self, job_path: Path, stock_path: Path, output_path: Path):
        self.job_path = job_path
        self.stock_path = stock_path
        self.output_path = output_path
        super().__init__("An expanded SSH-injected APFS ramdisk is required")


class IpswArchive(AbstractContextManager["IpswArchive"]):
    def __init__(self, source: str):
        self.source = source
        self.archive: zipfile.ZipFile | RemoteZip | None = None

    def __enter__(self) -> "IpswArchive":
        parsed = urllib.parse.urlparse(self.source)
        if parsed.scheme in {"http", "https"}:
            try:
                self.archive = RemoteZip(self.source, timeout=60)
            except Exception as exc:
                raise BuildError(f"Could not open remote IPSW: {exc}") from exc
        else:
            path = Path(self.source).expanduser().resolve()
            if not path.is_file():
                raise BuildError(f"IPSW file does not exist: {path}")
            try:
                self.archive = zipfile.ZipFile(path)
            except Exception as exc:
                raise BuildError(f"Could not open IPSW {path}: {exc}") from exc
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.archive is not None:
            self.archive.close()
        self.archive = None

    def read(self, member: str) -> bytes:
        if self.archive is None:
            raise BuildError("IPSW archive is not open")
        try:
            return self.archive.read(member)
        except KeyError as exc:
            raise BuildError(f"IPSW has no member: {member}") from exc
        except Exception as exc:
            raise BuildError(f"Could not fetch {member}: {exc}") from exc

    def fetch(self, member: str, destination: Path) -> Path:
        if destination.is_file() and destination.stat().st_size:
            return destination
        print(f"Fetching {member}...")
        data = self.read(member)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination


def parse_cpid(value: str) -> int:
    text = value.strip().lower()
    return int(text, 16 if not text.startswith("0x") else 0)


def firmware_catalog(product: str) -> list[dict[str, Any]]:
    url = f"https://api.ipsw.me/v4/device/{product}?type=ipsw"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        raise BuildError(f"Could not query ipsw.me for {product}: {exc}") from exc
    firmwares = data.get("firmwares", [])
    if not isinstance(firmwares, list):
        raise BuildError("ipsw.me returned an invalid firmware list")
    return firmwares


def choose_firmware(firmwares: list[dict[str, Any]], selection: str | None) -> dict[str, Any]:
    ordered = sorted(firmwares, key=lambda item: str(item.get("releasedate", "")), reverse=True)
    if selection:
        match = next(
            (
                item
                for item in ordered
                if str(item.get("buildid", "")) == selection or str(item.get("version", "")) == selection
            ),
            None,
        )
        if match is None:
            raise BuildError(f"No IPSW matches version/build: {selection}")
        return match

    print("Recent firmwares:")
    for index, item in enumerate(ordered[:25], 1):
        print(f"  {index:2d}) iOS {str(item.get('version', '')):12} {item.get('buildid', '')}")
    answer = input("Firmware [number | version | build]: ").strip()
    if answer.isdigit():
        index = int(answer)
        if not 1 <= index <= min(25, len(ordered)):
            raise BuildError("Invalid firmware selection number")
        return ordered[index - 1]
    return choose_firmware(ordered, answer)


def component_paths(manifest_data: bytes, board: str) -> tuple[dict[str, str], str]:
    try:
        manifest = plistlib.loads(manifest_data)
    except Exception as exc:
        raise BuildError(f"Invalid BuildManifest.plist: {exc}") from exc
    identities = manifest.get("BuildIdentities", [])
    identity = next(
        (item for item in identities if item.get("Info", {}).get("DeviceClass") == board),
        None,
    )
    if identity is None:
        raise BuildError(f"BuildManifest has no identity for board {board}")
    images = identity.get("Manifest", {})
    result: dict[str, str] = {}
    for output_name, candidates in REQUIRED_COMPONENTS.items():
        path = _first_manifest_path(images, candidates)
        if not path:
            raise BuildError(f"BuildManifest identity {board} has no {candidates[0]} path")
        result[output_name] = path
    for output_name, candidates in OPTIONAL_COMPONENTS.items():
        path = _first_manifest_path(images, candidates)
        if path:
            result[output_name] = path
    build = str(identity.get("Info", {}).get("BuildNumber", ""))
    return result, build


def _first_manifest_path(images: dict[str, Any], candidates: tuple[str, ...]) -> str:
    for candidate in candidates:
        path = images.get(candidate, {}).get("Info", {}).get("Path")
        if path:
            return str(path)
    return ""


def reset_output_dir(path: Path, allowed_root: Path) -> None:
    resolved = path.resolve()
    root = allowed_root.resolve()
    if root not in resolved.parents:
        raise BuildError(f"Refusing to reset path outside {root}: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def run_python(script: Path, *arguments: object, allow_failure: bool = False) -> bool:
    command = [sys.executable, str(script), *(str(value) for value in arguments)]
    print("Running:", " ".join(Path(part).name if index == 1 else part for index, part in enumerate(command)))
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(command, cwd=ROOT, check=False, env=environment)
    if result.returncode and not allow_failure:
        raise BuildError(f"{script.name} failed with exit code {result.returncode}")
    return result.returncode == 0


def resolve_kpf_set(version: str, has_txm: bool) -> str:
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        major = 0
    return "ios27" if has_txm or major >= 27 else "ios18"


def validate_ramdisk(data: bytes, source: Path, *, prepared: bool = False) -> None:
    if len(data) < 36 or data[32:36] != b"NXSB":
        raise BuildError(f"Prepared ramdisk is not a raw APFS container (NXSB missing): {source}")
    if prepared and len(data) < MIN_PREPARED_RAMDISK_BYTES:
        raise BuildError(
            f"Prepared ramdisk is only {len(data) / 1024 / 1024:.1f} MiB; "
            "the injected image must be expanded to at least 256 MiB"
        )
    if len(data) > MAX_RAMDISK_BYTES:
        raise BuildError(
            f"Prepared ramdisk is {len(data) / 1024 / 1024:.1f} MiB; remote boot limit is 280 MiB"
        )


def write_ramdisk_job(
    path: Path,
    *,
    stock: Path,
    output: Path,
    product: str,
    board: str,
    version: str,
    build: str,
) -> None:
    job = {
        "schema": 1,
        "product": product,
        "board": board,
        "version": version,
        "build": build,
        "stock_ramdisk": str(stock),
        "ssh_payload": str(RESOURCES / "ssh.tar.gz"),
        "restored_external": str(RESOURCES / "restored_external"),
        "expected_output": str(output),
        "requirements": [
            "expand APFS to 256-280 MiB",
            "extract ssh.tar.gz at filesystem root preserving POSIX modes and symlinks",
            "replace /usr/local/bin/restored_external",
            "ensure /usr/bin/mount_ich mode is 0755",
        ],
    }
    path.write_text(json.dumps(job, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an ICH A12/A13 bootchain on Windows")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--version")
    selection.add_argument("--build")
    parser.add_argument("--url", help="IPSW URL or local .ipsw path")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--product", help="offline product identifier, e.g. iPhone11,8")
    parser.add_argument("--board", help="offline board config, e.g. n841ap")
    parser.add_argument("--cpid", help="offline CPID, e.g. 0x8020")
    parser.add_argument("--im4m", type=Path)
    parser.add_argument("--prepared-ramdisk", type=Path, help="raw expanded APFS image with SSH injected")
    parser.add_argument("--kernel", choices=("stock", "patched"), default="patched")
    parser.add_argument(
        "--kpf-set",
        choices=("auto", "ios17", "ios18", "ios26", "ios27", "debugger+amfi", "all"),
        default="auto",
    )
    parser.add_argument("--use-ibss", action="store_true")
    parser.add_argument("--live-data", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    fw = parser.add_mutually_exclusive_group()
    fw.add_argument("--with-fw", dest="with_fw", action="store_true")
    fw.add_argument("--no-fw", dest="with_fw", action="store_false")
    parser.set_defaults(with_fw=True)
    return parser.parse_args()


def resolve_device(args: argparse.Namespace) -> tuple[str, str, int]:
    supplied = (args.product, args.board, args.cpid)
    if any(supplied):
        if not all(supplied):
            raise BuildError("--product, --board and --cpid must be supplied together")
        return args.product, args.board, parse_cpid(args.cpid)
    try:
        info = query_device(timeout=8)
    except IchUsbError as exc:
        raise BuildError(str(exc)) from exc
    if info is None:
        raise BuildError("No DFU device found; connect one or pass --product/--board/--cpid")
    if info.mode != "DFU" or info.pwned.lower() != "usbliter8":
        raise BuildError(f"Builder requires pwned DFU; got MODE={info.mode}, PWND={info.pwned or 'none'}")
    if info.product == "unknown" or info.board == "unknown" or info.cpid is None:
        raise BuildError("Connected device is not in the A12/A13 database")
    return info.product, info.board, info.cpid


def main() -> int:
    args = parse_args()
    try:
        product, board, cpid = resolve_device(args)
        selection = args.version or args.build
        print(f"Device: {product} / {board} / CPID 0x{cpid:04X}")

        if args.url:
            ipsw_source = args.url
            version = args.version or "unknown"
            build = args.build or "custom"
            if args.list:
                raise BuildError("--list cannot be combined with --url")
        else:
            firmwares = firmware_catalog(product)
            if args.list:
                for item in sorted(firmwares, key=lambda x: str(x.get("version", ""))):
                    print(f"{item.get('version', '')}\t{item.get('buildid', '')}\t{item.get('releasedate', '')}")
                return 0
            selected = choose_firmware(firmwares, selection)
            ipsw_source = str(selected.get("url", ""))
            version = str(selected.get("version", "unknown"))
            build = str(selected.get("buildid", "custom"))
        if not ipsw_source:
            raise BuildError("Selected firmware has no IPSW URL")

        cache = CACHE_ROOT / f"{product}-{build}"
        cache.mkdir(parents=True, exist_ok=True)
        manifest_path = cache / "BuildManifest.plist"
        with IpswArchive(ipsw_source) as ipsw:
            ipsw.fetch("BuildManifest.plist", manifest_path)
            paths, manifest_build = component_paths(manifest_path.read_bytes(), board)
            if build != "custom" and manifest_build and manifest_build != build:
                raise BuildError(f"BuildManifest build {manifest_build} does not match selected build {build}")
            if manifest_build:
                build = manifest_build

            has_sptm = "SPTM" in paths
            has_txm = "TXM" in paths
            kpf_set = resolve_kpf_set(version, has_txm) if args.kpf_set == "auto" else args.kpf_set
            print(f"Firmware: iOS {version} ({build})")
            print(f"Kernel: {args.kernel}, kpf-set={kpf_set}")
            print(f"Chain: iBSS={args.use_ibss}, SPTM={has_sptm}, TXM={has_txm}, with-fw={args.with_fw}")
            if args.dry_run:
                for key in sorted(paths):
                    print(f"  {key}: {paths[key]}")
                return 0

            required_keys = ["iBEC", "DeviceTree", "KernelCache", "RestoreRamDisk", "RestoreTrustCache"]
            if args.use_ibss:
                if "iBSS" not in paths:
                    raise BuildError("--use-ibss requested, but IPSW has no iBSS")
                required_keys.append("iBSS")
            required_keys.extend(key for key in ("SPTM", "TXM") if key in paths)
            if args.with_fw:
                for key in ("AOP", "ANE", "AVE", "ISP", "GFX", "SIO"):
                    if key not in paths:
                        raise BuildError(f"BuildManifest missing {key}, required by --with-fw")
                    required_keys.append(key)
                if "PMP" in paths:
                    required_keys.append("PMP")
            if "RestoreSEP" in paths:
                required_keys.append("RestoreSEP")
            elif args.live_data:
                raise BuildError("Selected IPSW has no RestoreSEP component")

            downloaded: dict[str, Path] = {}
            for key in required_keys:
                member = paths[key]
                downloaded[key] = ipsw.fetch(member, cache / Path(member).name)

        work = WORK_ROOT / f"windows-{product}-{build}"
        out = WORK_ROOT / f"windows-out-{product}-{build}"
        bootchain_name = f"{board}-{version}-{build}-ramdisk"
        bootchain = BOOTCHAIN_ROOT / bootchain_name

        # Read a prepared image before resetting scratch directories.  This
        # intentionally permits the path advertised by ramdisk-job.json under
        # work/: a second build must not erase that image before consuming it.
        prepared_ramdisk_data: bytes | None = None
        prepared_source: Path | None = None
        if args.prepared_ramdisk:
            prepared_source = args.prepared_ramdisk.expanduser().resolve()
            if not prepared_source.is_file():
                raise BuildError(f"Prepared ramdisk does not exist: {prepared_source}")
            prepared_ramdisk_data = extract_raw_or_img4(prepared_source)
            validate_ramdisk(prepared_ramdisk_data, prepared_source, prepared=True)

        reset_output_dir(work, WORK_ROOT)
        reset_output_dir(out, WORK_ROOT)

        for key, path in downloaded.items():
            shutil.copy2(path, work / f"{key}.im4p")

        extract_file(work / "iBEC.im4p", work / "iBEC.raw")
        extract_file(work / "KernelCache.im4p", work / "kernelcache.raw")
        stock_ramdisk = work / "ramdisk.stock.dmg"
        extract_file(work / "RestoreRamDisk.im4p", stock_ramdisk)
        stock_ramdisk_data = stock_ramdisk.read_bytes()
        validate_ramdisk(stock_ramdisk_data, stock_ramdisk)

        ramdisk = work / "ramdisk.dmg"
        if prepared_ramdisk_data is not None:
            if prepared_ramdisk_data == stock_ramdisk_data:
                raise BuildError(
                    "Prepared ramdisk is byte-for-byte identical to the stock image; "
                    "it does not contain the injected SSH payload"
                )
            ramdisk.write_bytes(prepared_ramdisk_data)
        else:
            job_path = work / "ramdisk-job.json"
            PREPARED_ROOT.mkdir(parents=True, exist_ok=True)
            prepared_target = PREPARED_ROOT / f"{product}-{board}-{version}-{build}.dmg"
            write_ramdisk_job(
                job_path,
                stock=stock_ramdisk,
                output=prepared_target,
                product=product,
                board=board,
                version=version,
                build=build,
            )
            raise RamdiskNeeded(job_path, stock_ramdisk, prepared_target)

        # Do not erase a previously working bootchain merely because the
        # preparation-only first pass was invoked without an injected image.
        reset_output_dir(bootchain, BOOTCHAIN_ROOT)

        ticket = args.im4m.expanduser().resolve() if args.im4m else RESOURCES / f"IM4M_0x{cpid:04X}"
        if not ticket.is_file():
            raise BuildError(f"Missing IM4M ticket: {ticket}")

        if args.use_ibss:
            extract_file(work / "iBSS.im4p", work / "iBSS.raw")
            run_python(PATCH / "iboot_patchfinder.py", work / "iBSS.raw", out / "iBSS.patched.raw", "--mode", "ibss")
            ok = run_python(
                PATCH / "finalize_iboot.py",
                "--stock",
                work / "iBSS.raw",
                "--input",
                out / "iBSS.patched.raw",
                "--output",
                bootchain / "iBSS.patched.bin",
                "--board",
                board,
                allow_failure=True,
            )
            if not ok:
                shutil.copy2(out / "iBSS.patched.raw", bootchain / "iBSS.patched.bin")
            (bootchain / "use-ibss").write_text("1\n", encoding="ascii")

        run_python(PATCH / "iboot_patchfinder.py", work / "iBEC.raw", out / "iBEC.patched.raw", "--mode", "ibec")
        run_python(
            PATCH / "finalize_iboot.py",
            "--stock",
            work / "iBEC.raw",
            "--input",
            out / "iBEC.patched.raw",
            "--output",
            out / "iBoot.patched.bin",
            "--board",
            board,
        )
        if args.use_ibss:
            wrap_raw(
                (out / "iBoot.patched.bin").read_bytes(),
                bootchain / "iBEC.patched.img4",
                ticket,
                fourcc="ibec",
            )

        if has_sptm:
            extract_file(work / "SPTM.im4p", work / "SPTM.raw")
            run_python(PATCH / "sptm_patchfinder.py", work / "SPTM.raw", out / "SPTM.patched.raw")
            wrap_raw((out / "SPTM.patched.raw").read_bytes(), bootchain / "sptm.img4", ticket, fourcc="sptm")
        if has_txm:
            extract_file(work / "TXM.im4p", work / "TXM.raw")
            run_python(PATCH / "txm_patchfinder.py", work / "TXM.raw", out / "TXM.patched.raw")
            wrap_raw((out / "TXM.patched.raw").read_bytes(), bootchain / "txm.img4", ticket, fourcc="trst")

        stock_tc = extract_im4p((work / "RestoreTrustCache.im4p").read_bytes())
        trustcache, hash_count = build_trustcache(
            stock_tc,
            RESOURCES / "ssh.tar.gz",
            RESOURCES / "sshtarlist.txt",
        )
        (work / "trustcache.bin").write_bytes(trustcache)
        print(f"Trust cache: appended {hash_count} CodeDirectory hashes")

        wrap_raw(trustcache, bootchain / "trustcache.img4", ticket, fourcc="rtsc")
        wrap_raw(ramdisk.read_bytes(), bootchain / "ramdisk.img4", ticket, fourcc="rdsk")
        wrap_existing_im4p(work / "DeviceTree.im4p", bootchain / "devicetree.img4", ticket, fourcc="rdtr")

        if args.with_fw:
            for component in FIRMWARE_COMPONENTS:
                source = work / f"{component}.im4p"
                if source.is_file():
                    wrap_existing_im4p(source, bootchain / f"{component}.img4", ticket)
            (bootchain / "with-fw.enabled").write_text("1\n", encoding="ascii")

        wrap_kernel(work / "kernelcache.raw", work / "KernelCache.im4p", bootchain / "kernelcache.img4.stock", ticket)
        run_python(
            PATCH / "apply_kernel_patches.py",
            work / "kernelcache.raw",
            "--output",
            out / "kernelcache.patched.raw",
            "--kpf-set",
            kpf_set,
            "--allow-missing",
        )
        wrap_kernel(
            out / "kernelcache.patched.raw",
            work / "KernelCache.im4p",
            bootchain / "kernelcache.img4.patched",
            ticket,
        )
        selected_kernel = bootchain / f"kernelcache.img4.{args.kernel}"
        shutil.copy2(selected_kernel, bootchain / "kernelcache.img4")
        (bootchain / "kernel.mode").write_text(f"{args.kernel}\n", encoding="ascii")
        (bootchain / "kpf.set").write_text(f"{kpf_set}\n", encoding="ascii")
        shutil.copy2(out / "iBoot.patched.bin", bootchain / "iBoot.patched.bin")

        if (work / "RestoreSEP.im4p").is_file():
            wrap_existing_im4p(work / "RestoreSEP.im4p", bootchain / "sep-firmware.img4", ticket)
        if args.live_data:
            (bootchain / "live-data.enabled").write_text("1\n", encoding="ascii")

        chain_info = {
            "product": product,
            "model": board,
            "cpid": f"0x{cpid:04X}",
            "chip": {0x8020: "A12", 0x8027: "A12X", 0x8030: "A13"}.get(cpid, "unknown"),
            "version": version,
            "build": build,
            "ibss": int(args.use_ibss),
            "sptm": int(has_sptm),
            "txm": int(has_txm),
            "kernel": args.kernel,
            "kpf_set": kpf_set,
            "with_fw": int(args.with_fw),
            "packaging": "pyimg4-with-im4m",
            "trustcache": "python-restore-append",
            "source": "windows-port-prepared-apfs",
        }
        (bootchain / "chain.info").write_text(
            "".join(f"{key}={value}\n" for key, value in chain_info.items()),
            encoding="utf-8",
        )

        preflight = [
            "--bootchain",
            bootchain,
            "--stock-iboot",
            work / "iBEC.raw",
            "--expected-board",
            board,
            "--expected-build",
            build,
            "--kernel-mode",
            args.kernel,
            "--stock-kernel",
            work / "KernelCache.im4p",
        ]
        if args.kernel == "patched":
            preflight.append("--allow-patched-kernel")
        run_python(PATCH / "preflight.py", *preflight)

        (ROOT / ".last_bootchain").write_text(f"{bootchain_name}\n", encoding="utf-8")
        print(f"Built: {bootchain}")
        print("Boot on Windows: .\\windows\\ich.ps1 boot")
        return 0
    except RamdiskNeeded as exc:
        print("\nAPFS preparation is required before the Windows build can continue.", file=sys.stderr)
        print(f"Stock image: {exc.stock_path}", file=sys.stderr)
        print(f"Job file:    {exc.job_path}", file=sys.stderr)
        print(f"Put the prepared image at: {exc.output_path}", file=sys.stderr)
        print("Prepare the image exactly as described, then rerun with:", file=sys.stderr)
        print(
            f"  .\\windows\\ich.ps1 build --prepared-ramdisk \"{exc.output_path}\" "
            "[repeat the same firmware options]",
            file=sys.stderr,
        )
        print("Do not use the unmodified stock image: it contains no SSH server.", file=sys.stderr)
        return 4
    except (BuildError, Img4ToolError, TrustCacheError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
