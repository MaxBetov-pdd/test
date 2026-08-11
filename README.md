# iOS 27 jailbreak with usbliter8 exploit

> **CAUTION!**
>
> Running this (restoring a custom firmware) will delete your entire device and break everything: SEP, passcode, Baseband, Bluetooth (partially work) and the entire Apple services, so please don't run it on your main device, ONLY do it on a spare device. This tutorial only targets developers that enjoy breaking their device!

![iPhone 11 on iOS 27.0 beta 4, running Sileo, with a root shell over SSH](images/iphone11-sileo.jpg)

Three device ports are present. The directories are **not** interchangeable: the display panel, board-tagged firmware filenames and offsets differ. The SE 2 port is experimental and its current limits are documented separately.

| Device | Board | iOS | Directory |
| :---- | :---- | :---- | :---- |
| **iPhone 11** | **`n104ap`** | **27.0 beta 4** (`24A5390f`) | **[`work-27.0b4-n104`](work-27.0b4-n104/)** |
| iPhone SE (2nd generation) | `d79ap` | 27.0 beta 4 (`24A5390f`) | [`work-27.0b4-d79`](work-27.0b4-d79/) **experimental** |
| iPhone 11 Pro / Pro Max | `d431ap` / `d421ap` | 27.0 beta 2, beta 3 | `work-27.0b2`, `work-27.0b3` |

Anything else requires finding the correct offsets to make it work.

## iPhone SE 2 (d79ap), iOS 27.0 beta 4

The d79 port has completed static verification and a real-device SSH ramdisk boot. PWN DFU, the iBSS/iBEC chain, firmware upload, the patched kernel, USB mux, SSH, and read-only mounts of `/mnt1` and `/mnt6` have been observed on an `iPhone12,8`.

Normal-boot jailbreak support is **not yet device-verified**. The guarded d79 tables currently cover 5 iBSS/iBEC patches, 9 TXM patches and 118 kernel patches, but a successful SSH ramdisk does not prove that a custom restore or normal boot is safe. Real SEP was not available in the tested SSH ramdisk, and `/mnt2` did not mount.

See the [d79 README](work-27.0b4-d79/README.md) for the exact verified/static-only/untested matrix and [port log](work-27.0b4-d79/D79_PORT.md) for device evidence. Do not follow the n104 restore tutorial on an SE 2.

## iPhone 11 (n104ap), iOS 27.0 beta 4

Everything that port needs is in [`work-27.0b4-n104/`](work-27.0b4-n104/): a [README](work-27.0b4-n104/README.md) that is nine ordered steps from firmware download to a booted device, and a [COMMANDS](work-27.0b4-n104/COMMANDS.md) cheatsheet for afterwards.

**Do not follow the tutorial further down this page for it.** That one is for the 11 Pro on beta 2, and several steps have no equivalent, including the APTicket re-dump, the bootstrap installer and the display fix.

Working: WiFi, 46 home screen icons, 266 apps registered, root shell over SSH, apt, Sileo including installs, TrollStore Lite installs apps that register and launch, root escalation from a non-root process, task ports on other processes, Camera and Photos, Safari downloads reaching Files, AirDrop, Siri, personas at boot.

Not working: setting wallpapers, pairing with a Mac, anything needing an Apple ID sign-in, tweak injection.

Two things about this port are worth knowing before you start, because both cost days to find:

- **The screen is black on every normal boot** until one word in iBSS is patched. USB, SSH and the kernel are all fine behind it, so it does not look like a display bug.
- **`AppleKeyStoreUserClient::externalMethod` needs a guarded shim, not the usual stub.** Returning unconditional success without writing the output buffer makes MobileKeyBag report "never unlocked since boot" permanently, which is what breaks the photo library, wallpapers and pairing.

Both are explained in that directory's README.

**Contributions welcome.** The open items are written up under [Open problems](work-27.0b4-n104/README.md#open-problems), each with the root cause where it is known, the offsets already derived, what has been ruled out, and the specific next step. Two are diagnosed down to a one-word fix and blocked only on delivery. Issues and PRs are both fine, and so is telling us we are wrong about something.

## iPhone 11 Pro / Pro Max, iOS 27.0 beta 2

The original port, by [34306](https://github.com/34306). The [Tutorial](#tutorial) below is written for these devices, and is his work along with everything it references in `patches/`.

[Hardware setup](#hardware-setup) applies to all ports: same rig, same wiring.

## Hardware setup

There's a SecureROM bug (released by Paradigm Shift) that requires the **RP2350** chip to exploit the device into PWN DFU mode. It only supports A12 and A13 (S4, S5 on Apple Watch series are also supported).

You need to drop the file from the original `usbliter8` source onto the board to make it run the exploit.

<p>
  <img src="images/image1.jpg" width="360" />
  <img src="images/image3.jpg" width="360" />
</p>

I use a **Raspberry Pi Pico 2** with RP2350 and a cut lightning cable:

- red → VBUS
- black → GND
- white (D-) → G13
- green (D+) → G12

## Downloads

**iPhone 11, 27.0 beta 4.** No direct link, because seed URLs rotate. `get_fw.py` resolves it from the device and build ID, downloads it, then checks the extracted `BuildManifest.plist` really says `24A5390f` / `iPhone12,1` / `n104ap` before letting the build continue:

```shell
brew install blacktop/tap/ipsw
cd work-27.0b4-n104 && ./get_fw.py
```

**iPhone 11 Pro, 27.0 beta 2.** IPSW from [Apple's website](https://updates.cdn-apple.com/2026SpringSeed/fullrestores/140-20242/CD53E584-98E6-4560-B847-D8D5027223E8/iPhone12,3,iPhone12,5_27.0_24A5370h_Restore.ipsw).

Install requirements:

```shell
pip3 install requests pyimg4 pymobiledevice3
```

Work inside `work-27.0b4-n104` for the 11, and `work-27.0b2` for the 11 Pro.

## Patches added

### iPhone 11 / 24A5390f

That port has its own set, too large to repeat here: 16 tables, around 180 patch words, listed in full in [`work-27.0b4-n104/README.md`](work-27.0b4-n104/README.md). The live source of truth is the code:

```shell
cd work-27.0b4-n104
./apply_patches.py --list                 # every table and its patch count
./apply_patches.py kc-boot kcache.raw     # dry run against a payload, writes nothing
```

Every patch carries the word it expects to overwrite and refuses to write if the file does not contain it, so a wrong build or a wrong file aborts at the desk instead of on the phone.

Three are worth knowing about even if you never open that directory:

| Component | Offset | Value | Notes |
| :---- | :---- | :---- | :---- |
| iBSS display init | `0x351c8` | `52800020` (`movz w0,#1`) | **n104 only.** Without it the screen is black on every normal boot while USB, SSH and the kernel are all fine. iBSS **only**, never iBEC |
| `AppleKeyStoreUserClient::externalMethod` | `0x213ac00` | guarded selector-7 shim | Not the usual `mov x0,#0; ret`. That returns success without writing the output buffer, so MobileKeyBag reads "never unlocked since boot" forever and Photos, wallpapers and pairing all break. The shim synthesises a valid lock state for selector 7 and leaves every other selector alone |
| boot-args | — | `backlight-level=1024` | n104 is an LCD and needs the backlight driven explicitly. The d421 OLED does not |

> Offsets are build-specific to **24A5390f / iPhone12,1 / n104ap**. They are not the same as the b2 numbers below.

### iPhone 11 Pro / 24A5370h

The original source from wh1te4ever included a lot of patches, you can read it in the code.

I added a few fixes to it to make it works:

| Component | Offset | Value | Notes |
| :---- | :---- | :---- | :---- |
| kernel `isDeviceInRestoreMode` | file `0x2894b68` (VA `0xFFFFFFF009898B68`) | `20 00 80 d2 c0 03 5f d6`| USB Restricted Mode bypass |
| kernel sandbox `file_check_mmap` | `0x2f774e0` | `00 00 80 d2 c0 03 5f d6` | Allow `/var/jb` execution (+ `mount_check_mount 0x2f75640`, `remount 0x2f75474`, `umount 0x2f75110`, `vnode_check_rename 0x2f7019c`) |
| kernel `AMFIIsCDHashInTrustCache` | `0x1f1ebe0` | `mov x0,#1;...` | Trust everything |
| DeviceTree `ephemeral-storage` | — | `u32=1` | Pass the 99% progress bar |
| `coreauthd` | `0x95c0` | `NOP` | Anti SEP crash |
| `ctkd` | `0x1b38/1b3c` | `mov x0,#0; ret` | Anti SEP crash |
| `mobileactivationd` `should_hactivate` | `0x2ebb14` | `20 00 80 52` (`mov w0,#1`) | Hacktivation |
| `mobileactivationd` `getActivationState` | `0x327cb0/d10/d14/d18` | `NOP/ADRP/ADD/NOP` → "Activated" | Belt-and-suspenders |
| launchd `disabled.plist` | — | 5 labels → `true` | Skip Setup (ScreenTimeAgent deadlock) |

The userland byte patches are scripted in [`patches/userland_patches.py`](patches/userland_patches.py) (`coreauthd`, `ctkd`, `mobileactivationd`). The launchd override is a plist edit, not a byte patch — see [`patches/disable_screentime.py`](patches/disable_screentime.py).

> Offsets are build-specific to **24A5370h / iPhone12,3**. Re-verify them in IDA for any other build.

## Tutorial

> **This tutorial is for the iPhone 11 Pro on beta 2.** For the **iPhone 11** on beta 4, follow [`work-27.0b4-n104/README.md`](work-27.0b4-n104/README.md) instead. Several steps below have no equivalent there, and several of its steps have no equivalent here: the APTicket re-dump, the bootstrap installer and the display fix.

Put the device in DFU mode, then plug it into the PWN DFU rig (the Raspberry Pi Pico 2 mentioned above).

> On the Pico 2, the light blinks twice while exploiting and stays lit on success. If the light turns off, the exploit failed, re-enter DFU mode and try again.

You can verify PWN mode by opening **System Configuration → USB tab → Apple Mobile Device (DFU Mode)**; if you see `PWND:[usbliter8]` then it worked.

### 1. Flash the Custom Firmware

After PWN DFU mode is done, plug the device back into the Mac, then:

```shell
cd work-27.0b2
./make_cfw.py            # requires sudo, enter your password
python3 tss_proxy_server.py && ./restore_cfw.sh
```

You'll see the restore progress bar on screen. Wait until the script is done and the device returns to recovery mode.

### 2. SSHRD boot

Re-enter DFU mode and PWN mode, plug it back into the Mac, then:

```shell
./get_rd.py
./boot_rd.sh
iproxy 2222 22 && ../tools/sshpass -p alpine ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 2222 root@localhost
```

On the SSH'd device, run:

```shell
/sbin/mount_apfs -o rdonly /dev/disk1s6 /mnt6
find /mnt6 -name sep-firmware.img4
```

`scp`/`cat` the file back to the Mac and name it `dev_sep.img4`. Back on the Mac:

```shell
../tools/img4tool -e -m t8030_apticket.der dev_sep.img4
```

The SSHRD log will print on screen, that means SSHRD succeeded.

### 3. Normal boot

```shell
./get_boot.py
./boot.py
```

That's the boot up. You can use SSH over dropbear and `iproxy` (default password `alpine`) to install Sileo with the bootstrap. Or you can do it over SSHRD.

### 4. Get past Setup

On the first normal boot the device lands in Setup and stays there. Two separate things are blocking it.

**a) Activation.**

```shell
./patches/userland_patches.py mobileactivationd mobileactivationd
./patches/userland_patches.py coreauthd coreauthd
./patches/userland_patches.py ctkd ctkd
# re-sign each one, keeping its original entitlements (keep a .orig backup first):
ldid -e mobileactivationd.orig > ents.plist
ldid -S ents.plist -Cadhoc mobileactivationd
```

**b) ScreenTime deadlock.** Setup still hangs on the loading spinner.

`ScreenTimeAgent` is an on-demand job (MachServices, has `.setup`). The fix is to make launchd refuse the launch, so Setup's XPC fails fast instead of hanging:

```shell
scp root@10.7.0.2:/var/db/com.apple.xpc.launchd/disabled.plist .
./patches/disable_screentime.py disabled.plist
scp disabled.plist root@10.7.0.2:/var/db/com.apple.xpc.launchd/disabled.plist
```

### 5. Internet + bootstrap

Wifi and baseband are all broken, so if you need internet to install things:

```shell
./net_up.sh
```

This automatically shares your Mac's internet to the device over USB. After that, do the bootstrap and Sileo will show up.

If Sileo does not show up, re-enter SSHRD mode and move `/var/jb/Applications/Sileo.app` to the `/Applications/` folder in `mnt1` (or `mnt2` depending on your apfs mount). Once you boot back to normal, `uicache` the device to let Sileo appear. The hook already works for the entire system.

You also need to fix symlinks for the bootstrap, check `bootstrap_1900.tar.zst`.

If you only get 3 apps on screen (Settings, Phone and Feedback), move all staged apps to `/Applications/` in the system folder (in SSHRD):

```shell
for a in /mnt2/staged_system_apps/*.app; do
  b=${a##*/}; [ -e /mnt1/Applications/$b ] || cp -R "$a" /mnt1/Applications/
done
```

Enjoy!


## Credits

I need to acknowledge and credit some awesome projects that I based this work on.

- [**usbliter8-fun**](https://github.com/wh1te4ever/usbliter8-fun) by [**wh1te4ever**](https://github.com/wh1te4ever) for CFW and Ramdisk patched for iOS 27.0 beta 2 (24A5370h). The iPhone 11 / beta 4 port in `work-27.0b4-n104` is derived from the beta 3 here
- [**34306**](https://github.com/34306) (Huy Nguyen) for the fork this one is built on: the tutorial, the `patches/` scripts
- [**Procursus**](https://github.com/ProcursusTeam) for the rootless bootstrap used by both ports
- [**khanhduytran0**](https://github.com/khanhduytran0) for idea on DeviceTree and USB Restriction in kernel
- **img4/img4tool** by [**tihmstar**](https://github.com/tihmstar) for sign IMG4 with APTicket
- **pyimg4/pymobiledevice3** by [**m1stadev**](https://github.com/m1stadev)/[**doronz88**](https://github.com/doronz88) for Export kernelcache, forward usbmux port
- **trollvnc** by [**Lakr233**](https://github.com/Lakr233) for Control device over USB
