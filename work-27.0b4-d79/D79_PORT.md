# d79ap port status

Target: iPhone SE (2nd generation), `iPhone12,8` / `d79ap`, A13/T8030,
iOS 27.0 build `24A5390f`.

## Verified locally

- The IPSW BuildManifest contains the expected d79 erase identity.
- d79 iBSS and iBEC are byte-identical: 2,481,856 bytes, SHA-256
  `2749ee482b68342df82ec48f37148c29c70fc9d347d0b20005a6ff6c2d5b8f71`.
- All five d79 iBSS/iBEC patches match pristine bytes.
- All 118 relocated d79 kernel patches match pristine bytes.
- TXM is byte-identical to n104 and all nine boot patches match.
- Both restore and normal-boot DeviceTree transformations pass structural verification.
- `get_fw.py` extracts component paths from the d79 erase identity without expanding the
  whole IPSW.

These checks prove that the patch tables target the intended binaries. They do not yet
prove that the phone reaches SSH; the first device boot is still required.

## Build without a Mac

The APFS ramdisk rebuild needs Apple's `hdiutil`, so Windows and Linux are used for USB
booting but not for this one build step. The repository contains
`.github/workflows/build-d79-sshrd.yml`, which runs that step on an arm64 macOS runner and
publishes `d79-24A5390f-sshrd` as an artifact.

The workflow reads the device/build-specific IM4M from the `D79_IM4M_B64` GitHub Secret.
The DER file is ignored by git and is not included in the artifact.

## When the phone is needed

Only after the workflow artifact exists:

1. Put the SE 2 into DFU and run usbliter8 until its serial reports `PWND:[usbliter8]`.
2. Boot the artifact's patched raw iBSS and the remaining IMG4 components.
3. Wait for USB/network SSH enumeration and collect the first kernel/launch logs.

Do not restore or modify the installed system for this first SSHRD test.
