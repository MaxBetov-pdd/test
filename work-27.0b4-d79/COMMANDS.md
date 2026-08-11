# d79 command reference

This file covers only operations verified for the iPhone SE 2 port. The old copy described a completed n104 jailbreak and was not valid for `d79ap`.

## Offline verification (no phone)

```sh
cd work-27.0b4-d79
python3 ./get_fw.py --ipsw ../iPhone12,8_27.0_24A5390f_Restore.ipsw
python3 ./verify_d79_port.py
```

Expected final line:

```text
[PASS] d79 offline port verification completed
```

## Inspect patch tables

```sh
python3 ./apply_patches.py --list
python3 ./apply_patches.py ibss-normal iBSS.raw
python3 ./apply_patches.py txm-boot TXM.raw
python3 ./apply_patches.py kc-boot kcache.raw
```

Omitting `-o` makes `apply_patches.py` read-only. It verifies the pristine hash, size, and guarded offsets, then writes nothing.

## Build the SSH ramdisk

Run the GitHub Actions workflow `Build d79 SSH ramdisk`. It needs the device/build wrapping manifest in the repository secret `D79_IM4M_B64` and publishes the artifact `d79-24A5390f-sshrd`.

This has been verified to boot on an SE 2. It does not install a jailbreak and does not modify the installed iOS system by itself.

On CachyOS/Linux, after downloading and entering the artifact directory:

```sh
sudo pacman -S --needed libirecovery python-pyusb libimobiledevice
bash ./boot_sshrd_linux.sh --irecovery /path/to/irecovery --expected-ecid YOUR_ECID
```

The loader follows both Recovery re-enumerations, validates `d79ap` and the optional ECID, waits for `05ac:12a8`, and saves the complete output under `logs/`.

## Connect after a successful SSHRD boot

Keep the proxy in one terminal:

```sh
iproxy 2222 22
```

Connect from another:

```sh
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 2222 root@localhost
```

Password: `alpine`.

Only read-only mounts have been verified on d79. Do not wait indefinitely on `/mnt2`: the tested ramdisk had no real SEP session and the Data mount blocked.

## Normal boot and restore

There is intentionally no d79 day-to-day jailbreak command yet.

- `get_boot.py` and `boot.py` contain the statically ported normal chain, but it has not completed a d79 device test.
- `make_cfw.py` and `restore_cfw.sh` are destructive and have not completed a d79 device test.
- n104 Sileo, TrollStore, activation, Wi-Fi, and tweak commands are not d79 results.

The first tethered normal attempt left iBoot, showed kernel verbose, then lost USB before normal-mode enumeration. Do not repeat the same artifact. The next experiment is the separate panic-preserving diagnostic artifact.

The Actions workflow prepares this as a separate artifact named `d79-24A5390f-normal-experimental`. Do not run it as though it were the verified SSH ramdisk. Its eventual Linux command is:

```sh
bash ./boot_normal_linux.sh --irecovery /path/to/irecovery --expected-ecid YOUR_ECID
```

That loader does not contain a restore path. It sends the tethered bootchain, records all USB transitions, and treats a new Recovery device as a failed normal boot.

For `d79-24A5390f-normal-diagnostic`:

```sh
bash ./boot_diag_linux.sh --irecovery /path/to/irecovery --expected-ecid YOUR_ECID
```

Keep the screen visible and photograph the last readable lines. The diagnostic build uses `kc-diag` and halt-on-panic boot arguments; no ramdisk, restore, mount, or filesystem write is present.
