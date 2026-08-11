# d79ap port evidence

Target: iPhone SE (2nd generation), `iPhone12,8` / `d79ap`, A13/T8030, iOS 27.0 build `24A5390f`.

Last updated: 2026-08-11.

## Verified offline

- The IPSW contains exactly one d79 erase identity for the expected product and build.
- d79 iBSS and iBEC extract to 2,481,856-byte payloads with SHA-256 `2749ee482b68342df82ec48f37148c29c70fc9d347d0b20005a6ff6c2d5b8f71`.
- All guarded d79 tables match pristine inputs: 5 iBSS/iBEC patches, 6/9 restore/boot TXM patches, and 20/118 restore/boot kernel patches.
- Both restore and normal-boot DeviceTree transformations serialize and pass structural post-checks.
- `get_fw.py` resolves component paths from `BuildManifest.plist`; it does not guess board filenames.

Run `python3 ./verify_d79_port.py` to reproduce these checks.

## Verified on a real SE 2

Device identity observed by the loader:

```text
PRODUCT: iPhone12,8
MODEL: d79ap
CPID: 0x8030
CHIP: A13
PWND: usbliter8
```

The tethered SSH ramdisk completed the following chain:

```text
PWN DFU -> iBSS -> native Recovery -> iBEC -> native Recovery
         -> firmware/DeviceTree/ramdisk/kernel -> bootx
         -> 05ac:12a8 -> iproxy -> root SSH
```

Inside the ramdisk, `uname` identified `PATCHED_ARM64_T8030 iPhone12,8`. `/mnt1` and `/mnt6` were mounted read-only.

## Known failed or blocked paths

- `AppleSEPManager` exposed `sep-booted = No` in the working SSH ramdisk.
- The device console repeatedly timed out waiting for `sep-endpoint:scrd`.
- `/mnt2` did not mount while SEP was unavailable.
- A stock restore `rspt/rtrx/rsep` experiment reached the kernel but did not enumerate final USB. This experiment is not part of the normal port and is not evidence that a stock SEP chain can trust modified userland.
- The first `kc-boot` tethered normal experiment completed every upload and `bootx`. The phone briefly showed verbose kernel output, then went black; Recovery USB disconnected and neither Linux nor Windows observed normal-mode `05ac:12a8` or any replacement Apple USB device. This localizes the failure after kernel entry but before normal userland USB enumeration. No restore or filesystem write occurred.

The `scrd` warning also appeared during boots where USB and SSH worked, so it must not be used alone to diagnose USB enumeration.

## Not yet verified

- successful d79 normal iOS boot with `ibss-normal`, `txm-boot`, `kc-boot`, and `patch_dt2.py`;
- the d79 display path during normal boot;
- custom restore/CFW;
- fake-SEP behavior after normal boot;
- Data mount, Setup, activation, Wi-Fi/baseband, bootstrap, Sileo, TrollStore, or tweaks.

Claims from the n104 port remain n104-only until a d79 device log proves them.

## Next milestone

Build and validate the separate `kc-diag` normal bootchain, then perform one diagnostic test that:

1. validates build, board, and ECID before upload;
2. captures a timestamped host log and every USB transition;
3. does not restore, erase, mount writable, or install anything;
4. keeps the first kernel panic visible and records whether any USB mode returns.

Only after that succeeds should System/Data modification be considered as a separate milestone.
