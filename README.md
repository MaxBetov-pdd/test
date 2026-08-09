# ICHA12A13 `v1.2`

SSH ramdisk for **Apple A12 / A13** after pwned DFU with [usbliter8](https://github.com/prdgmshift/usbliter8).

Made by **[@Official_I_C_H](https://t.me/Official_I_C_H)** · [t.me/Official_I_C_H](https://t.me/Official_I_C_H)

Not a jailbreak. Research use on devices you own.

Windows host port (experimental): [README-Windows.md](README-Windows.md).

Dedicated Linux Live USB (no Zadig/Apple driver switching):
[README-Live.md](README-Live.md).

If this helps you, please ⭐ **star the repo** — thanks.

## ☕ Buy Me a Coffee

If this project helped you, please consider supporting its development.

### USDT (TRC20)

**Wallet Address**
`TV3W882uz6n219dDgAntedV9o518Sqk255`

**Network:** TRON (TRC20)

Every contribution helps maintain and improve this project. Thank you! ❤️

## What’s new in v1.2

- **Automatic kernel patch path by iOS major** (A12 / A13)
- On-device **`mount_ich`** — mounts **all** filesystems over SSH (iOS **17 → 27+**)

| iOS | Kernel patches |
|-----|----------------|
| **17 / 18** | Proven finder (`PE_i_can_has_debugger` + `AMFIIsCDHashInTrustCache`) |
| **26** | Fixed byte-offset table (`patch/ios26_kernel_byte_patches.py`) |
| **27+** | Finder + launch constraints (TXM-era) |

iBoot includes verified XR `n841ap` / XS `d321ap` wrappers and a fail-closed
`d79ap` wrapper for mBoot-18000.122.4 / 23F84.  The d79 wrapper isolates the
restore boot-args reference from three unrelated users of the shared `%s`
string before `setenvnp` / `bootx`.

## Enter pwned DFU

1. DFU + **RP2350** + [usbliter8](https://github.com/prdgmshift/usbliter8)  
2. Cable to Mac (prefer **USB-A → Lightning**; USB-C adapters are flaky)  
3. Confirm:

```bash
./tools/darwin/irecovery -q
# MODE: DFU   PWND: usbliter8
```

DCSD/serial cables are fine for verbose UART, but **normal USB must reappear as Recovery** after iBoot. `./boot.sh` waits for that USB Recovery mode (and will prompt to unplug/replug if needed).

## Setup

```bash
./setup.sh
# or: brew install python@3 curl blacktop/tap/ipsw && pip3 install -r requirements.txt
```

## Quick start

```bash
./status.sh
./build.sh         # --kpf-set auto (default): 18→finder, 26→byte table
./boot.sh
```

SSH in, then mount everything:

```bash
iproxy 2222 22
ssh root@localhost -p 2222    # password: alpine
mount_ich                     # mounts all filesystems (System / Preboot / xART / Data / …)
```

`mount_ich` is the only mount command you need. It works on **A12 / A13** for **iOS 17 through 27+**.

If `./boot.sh` stops after “Boot triggered” / iBoot send: unplug and replug once when prompted, use a USB-A cable, and rebuild with `--with-fw`.

### Force a kernel path

```bash
./build.sh --build <BUILD> --with-fw --kpf-set ios18   # finder
./build.sh --build <BUILD> --with-fw --kpf-set ios26   # byte table
./build.sh --build <BUILD> --with-fw --kernel stock    # no kernel patches
```

If iOS 26 byte offsets do not match your exact kernel build, the build fails closed (safe). Update `patch/ios26_kernel_byte_patches.py` for that build, or pass finder fallback only after verifying.

## Layout

| Path | Role |
|------|------|
| `build.sh` / `boot.sh` | Build → boot; SSH + on-device `mount_ich` |
| `patch/iboot_patchfinder.py` | iBoot IMG4 / CTRR / boot-args |
| `patch/finalize_iboot.py` | `n841ap` / `d321ap` wrappers + `d79ap` 23F84 boot-args isolation |
| `patch/apply_kernel_patches.py` | Routes 18 vs 26 |
| `patch/ios26_kernel_byte_patches.py` | iOS 26-only offsets |
| `Darwin/` | kairos / cryptic (optional alternate iBoot tools) |
| `tools/darwin/` | img4, irecovery, usbliter8_boot, … |

## Notes

- Proven SSH path: **direct iBEC** (no iBSS required).
- RestoreSEP: `./boot.sh --sep` uses **`rsepfirmware`**.
- After SSH: run **`mount_ich`** once to mount all NAND filesystems.
