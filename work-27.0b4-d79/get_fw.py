#!/usr/bin/env python3
"""Validate the d79 IPSW and extract only what the selected build stage needs.

The default selective mode is sufficient for get_rd.py/get_boot.py and avoids expanding
the 10+ GiB IPSW. Use --full only when preparing a complete restore tree.
"""

import argparse
import plistlib
import shutil
import sys
import urllib.parse
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

DEVICE = "iPhone12,8"
BUILD = "24A5390f"
VERSION = "27.0"
BOARD = "d79ap"
EXTRACT_DIR = f"{DEVICE}_{VERSION}_{BUILD}_Restore"
PINNED_URL = (
    "https://updates.cdn-apple.com/2026SpringSeed/fullrestores/140-57338/"
    "E68F62EA-5270-4792-A89E-E09691874E08/"
    "iPhone12,8_27.0_24A5390f_Restore.ipsw"
)

HERE = Path(__file__).resolve().parent

# Every IPSW member consumed by get_rd.py or get_boot.py. Paths themselves come from
# BuildManifest; this list deliberately contains component names, not guessed filenames.
BOOT_COMPONENTS = (
    "iBSS",
    "iBEC",
    "LLB",
    "iBoot",
    "RestoreLogo",
    "ANE",
    "AOP",
    "AVE",
    "Ap,RestoreSecurePageTableMonitor",
    "Ap,RestoreTrustedExecutionMonitor",
    "GFX",
    "ISP",
    "PMP",
    "RestoreTrustCache",
    "SIO",
    "WCHFirmwareUpdater",
    "RestoreRamDisk",
    "RestoreDeviceTree",
    "RestoreSEP",
    "RestoreKernelCache",
)


def identity_for(manifest):
    matches = []
    for identity in manifest.get("BuildIdentities", []):
        info = identity.get("Info", {})
        if info.get("DeviceClass") == BOARD and info.get("RestoreBehavior") == "Erase":
            matches.append(identity)
    if len(matches) != 1:
        sys.exit(f"[!] expected one d79 erase identity, found {len(matches)}")
    return matches[0]


def validate_manifest(manifest):
    problems = []
    if manifest.get("ProductBuildVersion") != BUILD:
        problems.append(
            f"build is {manifest.get('ProductBuildVersion')!r}, expected {BUILD!r}")
    if DEVICE not in (manifest.get("SupportedProductTypes") or []):
        problems.append(f"firmware does not list {DEVICE}")
    identity = identity_for(manifest)
    if problems:
        sys.exit("[!] wrong firmware:\n    " + "\n    ".join(problems))
    return identity


def component_paths(identity):
    out = []
    manifest = identity.get("Manifest", {})
    for name in BOOT_COMPONENTS:
        try:
            path = manifest[name]["Info"]["Path"]
        except KeyError:
            sys.exit(f"[!] BuildManifest has no path for component {name!r}")
        if path not in out:
            out.append(path)
    return out


def find_archive(explicit, url):
    if explicit and url:
        sys.exit("[!] --ipsw and --url are mutually exclusive")
    if explicit:
        archive = Path(explicit).expanduser().resolve()
        if not archive.is_file():
            sys.exit(f"[!] IPSW not found: {archive}")
        return archive
    if url:
        return url

    candidates = []
    for base in (HERE, HERE.parent):
        candidates.extend(base.glob(f"*{BUILD}*.ipsw"))
    candidates = sorted({p.resolve() for p in candidates})
    if candidates:
        return candidates[0]
    return PINNED_URL


@contextmanager
def open_archive(source):
    parsed = urllib.parse.urlparse(str(source))
    if parsed.scheme in ("http", "https"):
        try:
            from remotezip import RemoteZip
        except ImportError:
            sys.exit("[!] remote IPSW extraction needs `pip install remotezip`")
        with RemoteZip(str(source), timeout=120) as archive:
            yield archive
    else:
        with zipfile.ZipFile(source) as archive:
            yield archive


def safe_extract_member(archive, member, dest):
    # IPSW names are POSIX paths regardless of host OS. Reject traversal before asking
    # zipfile to create anything.
    parts = PurePosixPath(member).parts
    if member.startswith("/") or ".." in parts:
        sys.exit(f"[!] unsafe member path in IPSW: {member!r}")
    target = dest / Path(PurePosixPath(member))
    target.parent.mkdir(parents=True, exist_ok=True)
    with archive.open(member) as source, target.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def verify_tree(dest, expected_paths):
    manifest_path = dest / "BuildManifest.plist"
    if not manifest_path.is_file():
        sys.exit(f"[!] missing {manifest_path}")
    with manifest_path.open("rb") as f:
        manifest = plistlib.load(f)
    validate_manifest(manifest)
    missing = [p for p in expected_paths if not (dest / Path(PurePosixPath(p))).is_file()]
    if missing:
        sys.exit("[!] selective extraction is incomplete:\n    " + "\n    ".join(missing))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ipsw", help="path to the local IPSW (auto-detected if omitted)")
    ap.add_argument("--url", help="remote IPSW URL; only selected members are downloaded")
    ap.add_argument("--full", action="store_true",
                    help="extract the whole IPSW instead of boot/SSHRD components only")
    args = ap.parse_args()

    archive_path = find_archive(args.ipsw, args.url)
    is_remote = urllib.parse.urlparse(str(archive_path)).scheme in ("http", "https")
    if args.full and is_remote:
        sys.exit("[!] --full requires a local IPSW; remote mode is selective by design")
    dest = HERE / EXTRACT_DIR
    marker = dest / ".extract-complete"
    previous_mode = marker.read_text(encoding="ascii").strip() if marker.is_file() else ""
    mode = "full" if args.full or previous_mode == "full" else "selective"
    print(f"[*] using {archive_path}")
    print(f"[*] extraction mode: {mode}")

    with open_archive(archive_path) as archive:
        try:
            manifest_bytes = archive.read("BuildManifest.plist")
        except KeyError:
            sys.exit("[!] IPSW has no BuildManifest.plist")
        manifest = plistlib.loads(manifest_bytes)
        identity = validate_manifest(manifest)
        selected = component_paths(identity)

        dest.mkdir(parents=True, exist_ok=True)
        if args.full:
            archive.extractall(dest)
        else:
            names = set(archive.namelist())
            metadata = [p for p in ("BuildManifest.plist", "Restore.plist",
                                    "SystemVersion.plist") if p in names]
            for member in metadata + selected:
                if member not in names:
                    sys.exit(f"[!] component listed by BuildManifest is absent: {member}")
                target = dest / Path(PurePosixPath(member))
                if not target.is_file() or target.stat().st_size != archive.getinfo(member).file_size:
                    print(f"[*] extracting {member}")
                    safe_extract_member(archive, member, dest)

    verify_tree(dest, selected)
    marker.write_text(mode + "\n", encoding="ascii")
    print(f"[+] verified {DEVICE} / {BOARD} / {VERSION} ({BUILD})")
    print(f"[+] ready at {dest}")


if __name__ == "__main__":
    main()
