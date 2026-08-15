#!/usr/bin/env python3.14
import struct
import os
import sys
import glob
import subprocess
from pathlib import Path

fp = None

def patch(offset, data):
    file_offset = offset
    
    if isinstance(data, int):
        data = struct.pack('<I', data)
    if isinstance(data, str):
        data = data.encode()

    fp.seek(file_offset)
    fp.write(data)
    fp.flush()

APPLY_PATCHES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apply_patches.py")
RAMDISK_PATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ramdisk_patch.py")


def apply_patches(table, path):
    """Verify and apply a patch table, aborting the build if anything is unexpected.

    Every offset is asserted against the byte it is meant to be overwriting. A wrong
    offset stops here instead of producing firmware that fails on the device.
    Offsets live in apply_patches.py, which asserts them; see also README.md.
    """
    rc = os.system(f'{sys.executable} "{APPLY_PATCHES}" {table} "{path}" -o "{path}"')
    if rc != 0:
        sys.exit(f"[!] patch verification failed for {table} on {path}, stopping.")

if not os.path.exists("Ramdisk"):
    os.system("mkdir Ramdisk")

# Copy the extracted IPSW into CFW, which is the slow part of the build (~10G).
#
# Two changes from upstream's silent `cp -rf`. rsync shows live progress, so an eight
# minute copy does not look like a hang. And completion is tracked with a sentinel file
# rather than by the existence of CFW/: a cancelled copy leaves a partial directory that
# an existence check would happily skip, silently building from a truncated IPSW.
# rsync also repairs such a partial copy instead of starting over.
#
# The extracted IPSW to build from, in this directory, exactly as ./get_fw.py leaves it.
# Same convention as b2/b3, which copy from e.g. iPhone12,3,iPhone12,5_27.0_24A5380h_Restore.
# Override with IPSW_SRC=/path/to/extracted if you keep firmware elsewhere.
IPSW_SRC = os.environ.get("IPSW_SRC", "iPhone12,8_27.0_24A5390f_Restore")
extract_mode = Path(IPSW_SRC, ".extract-complete")
_mode = extract_mode.read_text(encoding="ascii").strip() if extract_mode.is_file() else ""
# CFW_COMPONENTS_ONLY builds just the components the restore actually patches (iBEC,
# DeviceTree, the restore ramdisk, TXM, kernelcache, plus the raw iBSS usbliter8ctl boots)
# from a *selective* extraction. The stock ~10G RootFS is not patched and is not needed
# here; it is assembled onto a full IPSW tree on the machine that runs restore_cfw.sh. This
# keeps the build off a macOS runner's small disk and keeps the artifact small. Default
# (unset) still demands a full tree, which is what a local end-to-end restore needs.
COMPONENTS_ONLY = os.environ.get("CFW_COMPONENTS_ONLY") == "1"
if not COMPONENTS_ONLY and _mode != "full":
    sys.exit("[!] make_cfw.py needs a complete IPSW tree. Run ./get_fw.py --full first. "
             "The default selective extraction is intentionally only for SSHRD/boot. "
             "For a CI components-only build, run ./get_fw.py then set CFW_COMPONENTS_ONLY=1.")
COPY_DONE = "CFW/.copy-complete"
if not os.path.exists(COPY_DONE):
    if not os.path.isdir(IPSW_SRC):
        sys.exit(f"[!] extracted IPSW not found: {IPSW_SRC}\n"
                 f"    Run ./get_fw.py first, or set IPSW_SRC=/path/to/extracted/ipsw.")
    print("[*] copying IPSW -> CFW (~10G, the slow part)")
    # --chmod=Fu+w keeps CFW writable the way the old `cp -rf` left it. rsync -a would
    # otherwise preserve the source modes, and three files in the extracted IPSW
    # (Restore.plist, BuildManifest.plist, SystemVersion.plist) are r--r--r--.
    # Nothing in the build writes them today, but matching the previous behaviour
    # avoids a surprise later.
    if os.system(f"rsync -a --chmod=Fu+w --info=progress2 '{IPSW_SRC}/' CFW/") != 0:
        sys.exit("[!] IPSW copy failed, stopping.")
    Path(COPY_DONE).touch()
    print("[*] copy complete")
else:
    print("[*] CFW already present and complete, skipping copy")

# Patch iBSS 
if not os.path.exists("CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p -o Ramdisk/iBSS.raw")
apply_patches("ibss-restore", "Ramdisk/iBSS.raw")

# Patch iBEC
if not os.path.exists("CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak -o iBEC.raw")
apply_patches("ibss-restore", "iBEC.raw")
os.system("../tools/img4tool -c CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p -t ibec iBEC.raw")

# DeviceTree 
if not os.path.exists("CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/DeviceTree.d79ap.im4p CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak -o DeviceTree.raw")
# prevent data encryption for /private/var(/dev/disk1s2) and /private/var/mobile(/dev/disk1s8) # Disable "content-protect" in devicetree 
# also taken patches "no-effaceable-storage", "boot-ios-diagnostics" from https://github.com/TrungNguyen1909/qemu-t8030/blob/iOS16/hw/arm/xnu.c
# Abort if the patcher fails; upstream ignores the return code and would silently reuse
# a stale DeviceTree_patched.raw from an earlier run. Invoked via sys.executable so a
# missing execute bit on the script cannot break the build.
if os.system(f'{sys.executable} ./patch_dt.py DeviceTree.raw -o DeviceTree_patched.raw') != 0:
    sys.exit("[!] patch_dt.py failed, stopping.")
os.system("../tools/img4tool -c CFW/Firmware/all_flash/DeviceTree.d79ap.im4p -t dtre DeviceTree_patched.raw")


# RestoreRamdisk
#
# Upstream mounted this with `sudo hdiutil attach -mountpoint CFW_RD ... -owners off`,
# patched and re-signed in place, then detached. ramdisk_patch.py does the same work
# without sudo: the sudo was only needed for `-mountpoint <custom path>`, not for writing.
# It also signs each binary outside the image and writes the bytes back in place, which
# keeps the inode and therefore the original uid/gid/mode (ldid replaces the file rather
# than editing it, so a non-root re-sign would otherwise change ownership), and it
# preserves the code-signing identifier that a bare ldid would rewrite from the filename.
if not os.path.exists("CFW/094-13753-162.dmg.bak"):
    os.system("cp CFW/094-13753-162.dmg CFW/094-13753-162.dmg.bak")
os.system("pyimg4 im4p extract -i CFW/094-13753-162.dmg.bak -o ramdisk.dmg")
rc = os.system(f'{sys.executable} "{RAMDISK_PATCH}" --dmg ramdisk.dmg --apply '
               f'--repack-to CFW/094-13753-162.dmg '
               f'--orig-im4p CFW/094-13753-162.dmg.bak')
if rc != 0:
    sys.exit("[!] ramdisk patching failed, stopping.")


# TXM 
if not os.path.exists("CFW/Firmware/txm.iphoneos.release.im4p.bak"):
    os.system("cp CFW/Firmware/txm.iphoneos.release.im4p CFW/Firmware/txm.iphoneos.release.im4p.bak")
os.system("pyimg4 im4p extract -i CFW/Firmware/txm.iphoneos.release.im4p.bak -o TXM.raw")
# patch 
apply_patches("txm-restore", "TXM.raw")
#create im4p
os.system("pyimg4 im4p create -i TXM.raw -o TXM.im4p -d 1 -f trxm --lzfse")
# preserve payp structure
txm_im4p_data = Path('CFW/Firmware/txm.iphoneos.release.im4p.bak').read_bytes()
payp_offset = txm_im4p_data.rfind(b'PAYP')
if payp_offset == -1:
    print("Couldn't find payp structure !!!")
    sys.exit(1)

with open('TXM.im4p', 'ab') as f:
    f.write(txm_im4p_data[(payp_offset-10):])

payp_sz = len(txm_im4p_data[(payp_offset-10):])
print(f"payp sz: {payp_sz}")

txm_im4p_data = bytearray(open('TXM.im4p', 'rb').read())
txm_im4p_data[2:5] = (int.from_bytes(txm_im4p_data[2:5], 'big') + payp_sz).to_bytes(3, 'big')
open('TXM.im4p', 'wb').write(txm_im4p_data)
os.system("mv TXM.im4p CFW/Firmware/txm.iphoneos.release.im4p")



# Kernelcache
if not os.path.exists("CFW/kernelcache.release.iphone12c.bak"):
    os.system("cp CFW/kernelcache.release.iphone12c CFW/kernelcache.release.iphone12c.bak")
os.system("pyimg4 im4p extract -i CFW/kernelcache.release.iphone12c.bak -o kcache.raw")
apply_patches("kc-restore", "kcache.raw")

#create im4p
os.system("pyimg4 im4p create -i kcache.raw -o krnl.im4p -d KernelManagement_host-514 -f krnl --lzfse")

# preserve payp structure
kernel_im4p_data = Path('CFW/kernelcache.release.iphone12c.bak').read_bytes()
payp_offset = kernel_im4p_data.rfind(b'PAYP')
if payp_offset == -1:
    print("Couldn't find payp structure !!!")
    sys.exit(1)

with open('krnl.im4p', 'ab') as f:
    f.write(kernel_im4p_data[(payp_offset-10):])

payp_sz = len(kernel_im4p_data[(payp_offset-10):])
print(f"payp sz: {payp_sz}")

kernel_im4p_data = bytearray(open('krnl.im4p', 'rb').read())
kernel_im4p_data[2:6] = (int.from_bytes(kernel_im4p_data[2:6], 'big') + payp_sz).to_bytes(4, 'big')
open('krnl.im4p', 'wb').write(kernel_im4p_data)
os.system("mv krnl.im4p CFW/kernelcache.release.iphone12c")

# clean
os.system("rm iBEC.raw ramdisk.dmg TXM.raw kcache.raw DeviceTree_patched.raw")

# Final integrity check.
#
# apply_patches proves each patch landed in the intermediate file. This re-opens the
# components the restore will actually consume, decompresses their payloads and confirms
# every patch is still there. That covers repacking, PAYP re-append, ASN.1 length fixup
# and the ramdisk mount/re-sign/rebuild, none of which apply_patches sees.
print()
VERIFY_CFW = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "verify_cfw.py")
rc = os.system(f'{sys.executable} "{VERIFY_CFW}" '
               f'--dir "{os.path.dirname(os.path.abspath(__file__))}"')
if rc != 0:
    sys.exit("[!] CFW integrity check FAILED, do not flash this build.")
print("[+] build complete and verified, CFW is ready for restore_cfw.sh")
