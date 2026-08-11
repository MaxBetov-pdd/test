# iPhone SE 2 (`d79ap`) experimental port

iOS **27.0 beta 4** (`24A5390f`) on **iPhone SE (2nd generation)** (`iPhone12,8` / `d79ap` / A13 T8030), tethered through usbliter8.

This directory is a separate board port. Do not substitute files from `work-27.0b4-n104`, `work-27.0b2`, or `work-27.0b3`, even when a component happens to be byte-identical.

> **Destructive-operation warning**
>
> `restore_cfw.sh` erases the phone and installs an experimental custom firmware. It has not been device-tested on d79. Nothing in the verified SSH ramdisk result makes that restore safe. Back up the device and use only spare hardware if restore testing is eventually approved.

## Current status

| Area | Status | Evidence |
| :---- | :---- | :---- |
| IPSW identity | Verified | `iPhone12,8`, `d79ap`, `24A5390f` erase identity from `BuildManifest.plist` |
| PWN DFU | Verified on device | A13 reports `PWND: usbliter8` |
| SSH ramdisk boot | Verified on device | iBSS -> iBEC -> firmware -> patched kernel completed |
| Ramdisk USB and SSH | Verified on device | `05ac:12a8`, `iproxy`, root SSH |
| Read-only system mounts | Partially verified | `/mnt1` and `/mnt6` mounted read-only |
| Real SEP in SSH ramdisk | Not working | `AppleSEPManager` reported `sep-booted = No`; console repeated `waitForSEPEndpoint ... scrd` |
| Data volume (`/mnt2`) | Not working | mount blocked while SEP was unavailable |
| Normal-boot patch tables | Static verification only | pristine d79 iBSS/iBEC, TXM and kernel bytes match all guarded offsets |
| Normal iOS boot / jailbreak | First device attempt failed | patched kernel left iBoot, showed verbose, then lost USB before normal userland enumeration |
| Custom restore | Untested and destructive | do not infer support from SSHRD |
| Display, Wi-Fi, baseband, Sileo, tweaks, activation, Apple services | Untested on d79 | n104 results do not transfer automatically |

The exact device observations and known limitations are kept in [D79_PORT.md](D79_PORT.md).

## What is already ported

`apply_patches.py` contains d79-specific, original-byte-guarded patch tables. A wrong build or wrong offset aborts before writing.

| Table | Patches | Purpose |
| :---- | ----: | :---- |
| `ibss-restore` / `ibss-ramdisk` / `ibss-normal` | 5 each | Image4 validation and boot arguments |
| `txm-restore` | 6 | restore TXM policy |
| `txm-boot` | 9 | normal-boot TXM policy |
| `kc-restore` | 20 | restore kernel |
| `kc-boot` | 118 | normal kernel, including upstream fake-SEP/AKS behavior |
| `kc-diag` | 42 | diagnostic kernel that preserves useful failures |

Restore and normal-boot DeviceTree transforms are in `patch_dt.py` and `patch_dt2.py`. Board-specific n104 display patches are deliberately absent: no d79 display fix has been derived or proven necessary.

These tables establish that the intended instructions exist in the d79 binaries. They do **not** establish that the resulting normal boot works.

## Reproduce the static verification

Requirements:

```sh
python3 -m pip install pyimg4 remotezip requests
```

With the `24A5390f` IPSW in this repository or supplied explicitly:

```sh
cd work-27.0b4-d79
python3 ./get_fw.py --ipsw ../iPhone12,8_27.0_24A5390f_Restore.ipsw
python3 ./verify_d79_port.py
```

The verifier reads the d79 erase identity, extracts pristine payloads, checks every restore and normal-boot firmware table, then applies and structurally verifies both DeviceTree variants in a temporary directory. It never talks to a phone and never modifies the IPSW tree.

## Build the verified SSH ramdisk

The APFS ramdisk rebuild currently needs macOS `hdiutil`. GitHub Actions provides the reproducible build in `.github/workflows/build-d79-sshrd.yml`.

The workflow requires a per-device/per-restore wrapping manifest in the `D79_IM4M_B64` repository secret. `t8030_apticket.der` is deliberately ignored and must never be committed. The resulting `d79-24A5390f-sshrd` artifact is a tethered ramdisk, not a normal-boot jailbreak and not a custom restore.

The real-device SSHRD sequence has been verified from CachyOS using usbliter8 for the initial DFU upload and native `irecovery` after each USB re-enumeration. Keep complete host logs for every device test; USB bus/device numbers changing between stages is expected.

The artifact includes a Linux loader that waits for and validates each new USB transport instead of relying on fixed sleeps:

```sh
sudo pacman -S --needed libirecovery python-pyusb libimobiledevice
bash ./boot_sshrd_linux.sh --irecovery /path/to/irecovery --expected-ecid YOUR_ECID
```

Every run writes `logs/sshrd-YYYYMMDD-HHMMSS.log`. The ECID argument is optional for public artifacts but recommended as a safety lock when testing a specific phone.

Before opening USB, both Linux loaders verify `artifact-info.json`: product, board, build, mode, and the SHA-256 of every artifact file. An SSHRD artifact therefore cannot be accidentally passed to the normal loader or vice versa.

## Normal-boot development boundary

The first tethered, non-restore normal-boot experiment reached patched-kernel verbose output, then the display went black and USB disappeared before `05ac:12a8` normal userland enumeration. Neither Linux nor Windows saw a replacement USB device. Repeating that artifact cannot add evidence.

The same Actions workflow publishes a separate `d79-24A5390f-normal-experimental` artifact. It contains no restore ramdisk and its Linux loader never calls `idevicerestore`, `restore`, or a filesystem tool. It is kept separate from the verified SSHRD artifact so they cannot be confused. Device execution remains unverified and requires an explicit test decision.

The workflow also publishes `d79-24A5390f-normal-diagnostic`. It uses `kc-diag`, keeps the LCD backlight enabled, requests halt-on-panic, and omits launchd boot settings. Its purpose is to leave the first useful kernel failure visible on the phone; it is not expected to reach userland and is not a jailbreak payload. Run it with `bash ./boot_diag_linux.sh ...` and photograph the final readable lines.

The upstream jailbreak substitutes kernel/TXM behavior for missing SEP/AKS functionality. That may make local SSH, bootstrap, Sileo, and tweaks possible, but it does not create a real SEP session and does not guarantee passcode, Data-volume access, activation, iCloud, push notifications, Apple Pay, or other Apple services.

No script should perform any of the following as part of a first normal-boot test:

- restore or erase the phone;
- write to System, Data, xART, or SEP-related storage;
- remove `Setup.app` or change activation records;
- install a bootstrap or launch daemon.

Those are later, separately approved milestones after a normal kernel and USB/SSH are proven.

## File map

```text
get_fw.py             validate/selectively extract the d79 IPSW
verify_d79_port.py    offline verification of firmware and DeviceTree patches
write_artifact_info.py reproducible artifact identity and SHA-256 manifest
apply_patches.py      guarded d79 patch tables
patch_dt.py           restore/SSHRD DeviceTree transform
patch_dt2.py          normal-boot DeviceTree transform
get_rd.py             build SSH ramdisk components (macOS build host)
boot_sshrd_linux.py   transition-aware CachyOS/Linux SSHRD loader
boot_sshrd_linux.sh   timestamped host-log wrapper
boot_normal_linux.py  one non-restore normal-boot experiment
boot_normal_linux.sh  timestamped normal-boot log wrapper
get_boot.py           prepare normal-boot components; not yet device-verified
make_cfw.py           destructive CFW preparation; not yet device-verified
verify_cfw.py         inspect a prepared CFW tree
D79_PORT.md           evidence and port progress
```

## Contributions

When reporting a d79 test, include the exact build, ECID redacted if desired, full host log, USB VID:PID transitions, and whether the result came from SSHRD, tethered normal boot, or restore. A screen photo alone is not enough to distinguish a kernel failure from a USB transport failure.

This port derives from the n104 work and the original iPhone 11 Pro/Max usbliter8-fun ports. Their credits remain in the repository root README.
