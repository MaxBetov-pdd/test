# COMMANDS

Cheatsheet for day to day use, once the port is already built and flashed. If you are setting up from scratch, follow `README.md` first: this file assumes the CFW exists, the APTicket is current, and the bootstrap is installed.

Everything here runs from this directory. The device is tethered, so every boot needs the RP2350 rig and a DFU cycle.

---

## 1. Boot

Put the device in pwn DFU first. `get_rd.py` and `get_boot.py` both overwrite `Ramdisk/`, so whichever ran last is what boots.

```sh
# normal boot, into iOS
./get_boot.py && ./boot.py

# SSH ramdisk, for anything that writes to the System volume
./get_rd.py && ./boot_rd.sh

# which chain is staged right now
ls Ramdisk/ | grep -c RestoreRamdisk      # 1 = SSHRD, 0 = normal
```

Black screen on a normal boot is almost always a stale APTicket. See section 6.

---

## 2. SSH

Works the same on a normal boot and in SSHRD. Password is `alpine`.

```sh
iproxy 2222 22 &

../tools/sshpass -p alpine ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -p 2222 root@localhost
```

The System volume has almost no commands, so export the bootstrap path before anything else:

```sh
export PATH=/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:/usr/bin:/bin:/usr/sbin:/sbin
```

`./setup_shell.sh` does this permanently, along with dropping you into zsh and putting an `[SSHRD]` marker in the prompt. It only needs running once per restore. Everything below still works without it.

There is no `scp`, `plutil`, `sqlite3`, `python3` or `pluginkit` on the device. Move files with a pipe instead.

Push:

```sh
../tools/sshpass -p alpine ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -p 2222 root@localhost \
    'cat > /var/jb/usr/bin/mytool' < mytool
```

Pull:

```sh
../tools/sshpass -p alpine ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -p 2222 root@localhost \
    'export PATH=/var/jb/usr/bin:/var/jb/bin:/usr/bin:/bin; cat /var/mobile/jbboot.log' \
    > jbboot.log
```

**The `export PATH` in the pull is not optional.** `cat` lives in the bootstrap, not on the System volume. Without it the remote command fails, its error goes to stderr, and the redirect still creates a perfectly normal looking **empty file**. You get a zero byte result and no visible error. Check the size:

```sh
wc -c < jbboot.log
```

Verify a pushed binary arrived intact rather than trusting the exit code:

```sh
../tools/sshpass -p alpine ssh -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -p 2222 root@localhost \
    'export PATH=/var/jb/usr/bin:/var/jb/bin:/usr/bin:/bin
     chmod 755 /var/jb/usr/bin/mytool; sha256sum /var/jb/usr/bin/mytool'
shasum -a 256 mytool
```

---

## 3. Volume layout

```
SSHRD     /        = /dev/md0, read only
          /mnt1    = System   (disk1s1)
          /mnt2    = Data     (disk1s2)
          /mnt6    = Preboot

normal    /        = System, read only
          /var     = Data, read write
          /var/jb  = bootstrap
```

Mount by hand in SSHRD if a script has not already done it:

```sh
mkdir -p /mnt1 /mnt2
/sbin/mount_apfs /dev/disk1s1 /mnt1
/sbin/mount_apfs /dev/disk1s2 /mnt2
/sbin/mount -u -o rw /dev/disk1s1        # System writable
```

`sshrd_provision.sh` does all of this in its `mounts` step and refuses to run if it finds itself on a normal boot.

---

## 4. Provisioning, in SSHRD

```sh
./sshrd_provision.sh --check             # report only, changes nothing
./sshrd_provision.sh                     # everything
./sshrd_provision.sh cache jbtools       # only these steps
```

Steps: `mounts cache jbtools sileo resolv apps verify`.

TrollStore is not one of them any more. It installs after first boot, see section 5.

Missing payload binaries are skipped rather than treated as errors, so check the output. A silently absent `personaalloc` shows up later as a persona that never appears, not as a failure.

---

## 5. On a normal boot

```sh
uicache -a -f                            # register apps, rebuilds the databases
killall -9 SpringBoard                   # respring
```

`uicache -p` cannot register anything on iOS 27. Only `-a` works, and only for bundles in `/Applications`.

**Stop running `uicache -a -f` once TrollStore is installed.** It re-registers every bundle it finds as a *User* app, which silently downgrades everything under `/var/containers` from `System` to `User`. Those apps then fail to launch and their icons disappear, which looks like an unrelated SpringBoard problem. Recover with:

```sh
trollstorehelper refresh                 # puts them back to System
```

It is safe on a fresh device, before any TrollStore apps exist. After that, use `trollstorehelper refresh` instead.

Check the boot job did its work:

```sh
cat /var/mobile/jbboot.log               # written by boot/jbboot.sh each boot
personaalloc                             # expect "YES: id=99"
```

### TrollStore Lite, after first boot

Not installed by provisioning. It goes on here instead, so it lives in `/var/containers` rather than on the sealed System volume and can be updated without another DFU trip.

Needs `ldid`, which is not in the bootstrap, so this step needs working DNS (section 8):

```sh
apt install -y ldid
```

Get the package from the fork's release page, on the Mac:

<https://github.com/Xplo8E/TrollStore27/releases>

```sh
com.opa334.trollstorehelper27_2.1.1-ios27+2_iphoneos-arm64.deb   # the helper, installs the app
TrollStoreLite-ios27.ipa                                         # the app on its own, optional
SHA256SUMS                                                       # check what you downloaded
```

Then push it and let it install itself. `scp` does not work on this device, there is no `sftp-server`, so use an SSH redirect:

```sh
shasum -a 256 -c SHA256SUMS
ssh -p 2222 root@localhost "cat > /var/tmp/ts27.deb" < com.opa334.trollstorehelper27_2.1.1-ios27+2_iphoneos-arm64.deb
ssh -p 2222 root@localhost "apt install -y /var/tmp/ts27.deb"
```

The package installs the helper to `/var/jb/usr/bin/trollstorehelper` and its `postinst` uses that helper to install the app, so registration goes through the patched code path rather than `uicache`. Expect `TrollStore Lite installed.` and an app registered as `System`.

Confirm:

```sh
dpkg -l | grep trollstore                # expect ii com.opa334.trollstorehelper27
lsprobe --lookup com.opa334.TrollStoreLite   # expect applicationType: System
```

The stock TrollStore from opa334's releases does **not** work here. Its helper still calls `registerApplicationDictionary:`, which is a stub returning NO on iOS 26+, so apps install and never appear. The package above is that same helper with four fixes on top.

The IPA on that release page is self-bootstrapping if you would rather not use the deb: the helper needed to install it is already inside it at `Payload/TrollStoreLite.app/trollstorehelper`.

---

## 6. APTicket, re-dump after every restore

Not optional. A stale ticket boots to a black screen, and the ticket is bound to both your device and the restore.

```sh
# in SSHRD
find /mnt6 -name sep-firmware.img4

# pull that file back here as dev_sep.img4, then on the Mac
../tools/img4tool -e -m t8030_apticket.der dev_sep.img4
```

---

## 7. Clean restore

Erases the device. The TSS proxy has to be running or `idevicerestore` fails immediately.

```sh
python3 tss_proxy_server.py &
./restore_cfw.sh
```

If it panics with "enter restore mode", cold reset the device and retry.

After a restore you need, in order: APTicket (section 6), `patch_setup.py --apply`, `install_bootstrap.sh`, `install_dropbear.sh`, `sshrd_provision.sh`.

---

## 8. Packages, and DNS

```sh
apt update
apt install <pkg>
```

Both `apt` and Sileo work.

If Sileo installs fail with `Unable to fetch some archives`, it is not a network fault. Sileo downloads as `mobile` and only installs as root, so the apt archive dir has to be writable by `mobile`:

```sh
chown 501:501 /var/jb/var/cache/apt/archives /var/jb/var/cache/apt/archives/partial
```

`install_bootstrap.sh` does this now, so it should only affect devices provisioned before that. Root keeps write access regardless of owner, which is why CLI `apt` never shows the problem.

If Sileo reports a package as unavailable, kill it, reopen, and pull to refresh.

### Repositories

They live in `/var/jb/etc/apt/sources.list.d/` on the device:

```sh
ls /var/jb/etc/apt/sources.list.d/
```

Adding one:

```sh
printf 'Types: deb\nURIs: %s\nSuites: ./\nTrusted: yes\n' 'https://example.com/repo/' \
    > /var/jb/etc/apt/sources.list.d/example.sources
apt update
```

deb822 (`.sources`) rather than one-line `.list`. Both parse, but apt reads both, so an old `.list` for the same repo warns about duplicate sources on every update. Rename it if you have one.

`Trusted: yes` disables signature checking. Needed because these repos ship no key apt holds, and it refuses unsigned sources without it.

### If name resolution fails

WiFi itself is fine. The catch is that command line tools resolve through `/private/etc/resolv.conf`, which the stock image does not ship and which sits on the read-only System volume. The bootstrap has its own at `/var/jb/etc/resolv.conf`, but nothing consults that path.

The `resolv` step of `sshrd_provision.sh` writes the real file, so normally there is nothing to do. Check it first:

```sh
cat /private/etc/resolv.conf             # expect at least one nameserver
```

If it is missing or empty and you do not want an SSHRD trip to fix it, `aptproxy.py` does the resolution on the Mac instead. Run it there:

```sh
./aptproxy.py
[+] proxy listening on 10.0.0.5:8899
[.] point apt at:  Acquire::http::Proxy "http://10.0.0.5:8899";
```

It detects this machine's LAN address at startup and prints the exact apt line to use. Paste that on the device. `/var/jb/etc/apt/apt.conf.d/` is on the Data volume and writable on a normal boot:

```sh
echo 'Acquire::http::Proxy "http://10.0.0.5:8899";' \
    > /var/jb/etc/apt/apt.conf.d/00proxy.conf
```

Remove that file once `resolv.conf` is in place, or apt keeps going through a proxy that is no longer running.

If the device reaches the Mac over an `ssh -R 8899:127.0.0.1:8899` tunnel rather than WiFi, bind to loopback instead:

```sh
./aptproxy.py --bind 127.0.0.1
```

The tunnel delivers to `127.0.0.1`, so a proxy bound only to the LAN address never sees the traffic. That one cost an afternoon and looked like a dropbear bug.

This is a fallback, not part of the normal flow. It predates the `resolv` step and is kept because it needs no reboot.

---

## 9. Logs, without pairing

The Mac cannot pair with this device, so `idevicesyslog` is unavailable. The whole unified log store is readable over root SSH instead:

```sh
ssh ... 'export PATH=/var/jb/usr/bin:$PATH; ls -la /var/db/diagnostics/Persist/'
```

Copy the store off and read it on the Mac. It is around 700 MB.

Crash reports are plain files:

```sh
ls -lat /var/mobile/Library/Logs/CrashReporter/*.ips | head
```

---

## 10. Verifying patches

```sh
./apply_patches.py --list                        # every table and its patch count
./apply_patches.py kc-boot kcache.raw            # dry run against a payload
./verify_cfw.py                                  # re-open a built CFW, confirm patches survived
```

Every patch asserts the word it expects to overwrite, so a wrong build or a wrong file aborts instead of writing. `verify_cfw.py` reports each patch as ALREADY APPLIED; anything still pending means that component was rebuilt from a pristine payload and the patch was lost.

Check the AKS shim landed, on a normal boot, with no injection or helper:

```
MKBDeviceUnlockedSinceBoot()  should return 1
```

If it returns 0, `kc-boot` did not apply or the wrong kernelcache booted. `MKBGetDeviceLockState` returning 0 is expected and not a fault: it reaches AppleKeyStore by a path the shim deliberately does not touch.

---

## 11. Things that are not true

Each of these cost real time.

- `uicache -p` does not register anything on iOS 27. Use `-a`.
- `uicache -a -f` is not a safe repair once TrollStore apps exist. It downgrades every bundle under `/var/containers` from `System` to `User` and they stop launching. Use `trollstorehelper refresh`.
- `registerApplicationDictionary:` does not register anything on iOS 26+. It is a stub that always returns NO, so an app installs, reports success, and never appears. The working call is `registerContainerizedApplicationWithInfoDictionaries:`, and it is entitlement-gated.
- Testing persona escalation as `root` proves nothing. Root is exempt from the check, so it succeeds whether or not the kernel is patched. Test as `mobile`.
- `/var/staged_system_apps` is never scanned for registration. Apps must be in `/Applications`.
- `launchd_no_cache=1` does not restore plist scanning. `launchd_unsecure_cache=1` is the one that matters, and it is read by `/usr/libexec/launchd_cache_loader`, not by `launchd`.
- launchd cannot exec bootstrap binaries. A boot job needs the system `/bin/sh`, and `/bin` on the System volume has five commands.
- `tr` does not exist on the ramdisk.
- An unmounted directory and an empty one look identical to `ls`. Check `mount` before believing a negative.
- Applying `ibss-skip-display-init` to iBEC as well as iBSS disables the init it exists to let succeed.
