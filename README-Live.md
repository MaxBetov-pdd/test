# ICH SE2 Live USB

`ICH SE2 Live` is a Debian 12 XFCE environment dedicated to the current
iPhone SE 2020 target:

- product `iPhone12,8`, board `d79ap`, A13 (`CPID 0x8030`);
- ECID `00094D3A3A6B802E`;
- restore build `23F84`;
- RP2350/usbliter8 PWND DFU.

The ISO includes the bootchain produced by the existing `build-23f84` job,
PyUSB/libusb, automatic DFU/Recovery udev permissions, usbmuxd, `iproxy`, SSH,
and the `ich` command. It never needs Zadig or an Apple Windows driver.

## Build in GitHub Actions

Run the workflow **Build 23F84 ramdisk and Live USB**. Download the artifact
`ICH-SE2-Live-23F84`; it contains:

- `ich-se2-live-amd64.iso`;
- `ich-se2-live-amd64.iso.sha256`.

Write the ISO to a USB drive with Rufus in DD mode, balenaEtcher, or from Linux:

```bash
sudo dd if=ich-se2-live-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

`/dev/sdX` must be the whole USB drive. This command overwrites that drive.

## Boot and use

Disable Secure Boot if the firmware refuses the unsigned Debian Live image,
then boot the USB drive. The XFCE desktop opens an ICH terminal automatically.

```bash
ich doctor
ich status
ich boot --with-fw
```

If the ramdisk enumerates, open another terminal:

```bash
ich iproxy 2222 22
ssh root@localhost -p 2222
```

Password: `alpine`.

The Live image is fail-closed for the configured board and ECID. It does not
write NVRAM or phone storage by itself. The current unresolved mBoot `bootx`
handoff remains an independent target-side issue; the Live environment removes
Windows USB-driver variables but does not claim to bypass that issue.
