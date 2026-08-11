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

BOOT_VARIANT = os.environ.get("D79_BOOT_VARIANT", "normal")
BOOT_TABLES = {
    "normal": ("ibss-normal", "kc-boot"),
    "diagnostic": ("ibss-normal-diag", "kc-diag"),
}
if BOOT_VARIANT not in BOOT_TABLES:
    sys.exit("[!] D79_BOOT_VARIANT must be 'normal' or 'diagnostic'")
IBOOT_PATCH_TABLE, KERNEL_PATCH_TABLE = BOOT_TABLES[BOOT_VARIANT]
print(
    f"[*] d79 boot variant: {BOOT_VARIANT} "
    f"(iBoot={IBOOT_PATCH_TABLE}, kernel={KERNEL_PATCH_TABLE})"
)


def apply_patches(table, path):
    """Verify and apply a patch table, aborting the build if anything is unexpected.

    Every offset is asserted against the byte it is meant to be overwriting. A wrong
    offset stops here instead of producing firmware that fails on the device.
    Offsets live in apply_patches.py, which asserts them; see also README.md.
    """
    rc = os.system(f'{sys.executable} "{APPLY_PATCHES}" {table} "{path}" -o "{path}"')
    if rc != 0:
        sys.exit(f"[!] patch verification failed for {table} on {path}, stopping.")

os.system("rm -rf Ramdisk")
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

# 1. Grab & Patch iBSS 
if not os.path.exists("CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/dfu/iBSS.d79.RELEASE.im4p -o Ramdisk/iBSS.raw")
apply_patches(IBOOT_PATCH_TABLE, "Ramdisk/iBSS.raw")
# The n104-only display-init patch is deliberately not applied on d79.

# 2. Grab & Patch iBEC
# FYI, iBEC load address = 0x19c050000
if not os.path.exists("CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak")
# Read from the .bak. make_cfw.py writes its restore-patched iBEC back over the
# non-.bak path, so reading that would build the normal-boot iBEC on top of an
# already-restore-patched binary. get_rd.py already reads .bak; this matches it.
os.system("../tools/img4 -i CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak -o iBEC.raw")
apply_patches(IBOOT_PATCH_TABLE, "iBEC.raw")
os.system("../tools/img4tool -c iBEC.im4p -t ibec iBEC.raw")
os.system("../tools/img4 -i iBEC.im4p -o Ramdisk/iBEC.img4 -M t8030_apticket.der")

# 1. Grab & Patch iBSS 
if not os.path.exists("CFW/Firmware/all_flash/LLB.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/LLB.d79.RELEASE.im4p CFW/Firmware/all_flash/LLB.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/LLB.d79.RELEASE.im4p -o Ramdisk/LLB.raw")

# 2. Grab & Patch iBoot 
if not os.path.exists("CFW/Firmware/all_flash/iBoot.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/iBoot.d79.RELEASE.im4p CFW/Firmware/all_flash/iBoot.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/iBoot.d79.RELEASE.im4p -o Ramdisk/iBoot.raw")

# 3. Grab AppleLogo
if not os.path.exists("CFW/Firmware/all_flash/applelogo@2x~iphone.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/applelogo@2x~iphone.im4p CFW/Firmware/all_flash/applelogo@2x~iphone.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/applelogo@2x~iphone.im4p.bak -o Ramdisk/RestoreLogo.img4 -M t8030_apticket.der -T rlgo")

# 4. Grab Other Components...
# ANE
if not os.path.exists("CFW/Firmware/ane/h12_ane_fw_metis.im4p.bak"):
    os.system("cp CFW/Firmware/ane/h12_ane_fw_metis.im4p CFW/Firmware/ane/h12_ane_fw_metis.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/ane/h12_ane_fw_metis.im4p.bak -o Ramdisk/ANE.img4 -M t8030_apticket.der -T anef")

# AOP
if not os.path.exists("CFW/Firmware/AOP/aopfw-iphone12caop.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/AOP/aopfw-iphone12caop.RELEASE.im4p CFW/Firmware/AOP/aopfw-iphone12caop.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/AOP/aopfw-iphone12caop.RELEASE.im4p.bak -o Ramdisk/AOP.img4 -M t8030_apticket.der -T aopf")

# AVE
if not os.path.exists("CFW/Firmware/ave/AppleAVE2FW_H12.im4p.bak"):
    os.system("cp CFW/Firmware/ave/AppleAVE2FW_H12.im4p CFW/Firmware/ave/AppleAVE2FW_H12.im4p.bak")
# Upstream has the next line indented into the block above, so AVE.img4 is only built on
# the first run, when the .bak does not yet exist. Every other component builds it
# unconditionally. Since this script also starts with `rm -rf Ramdisk`, running get_rd.py
# first (which creates the .bak) leaves AVE.img4 permanently missing and boot.py then
# fails on `irecovery -f Ramdisk/AVE.img4`.
os.system("../tools/img4 -i CFW/Firmware/ave/AppleAVE2FW_H12.im4p.bak -o Ramdisk/AVE.img4 -M t8030_apticket.der -T avef")

# SPTM
if not os.path.exists("CFW/Firmware/sptm.t8030.release.im4p.bak"):
    os.system("cp CFW/Firmware/sptm.t8030.release.im4p CFW/Firmware/sptm.t8030.release.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/sptm.t8030.release.im4p.bak -o Ramdisk/SPTM.img4 -M t8030_apticket.der -T sptm")

# TXM with patching
if not os.path.exists("CFW/Firmware/txm.iphoneos.release.im4p.bak"):
    os.system("cp CFW/Firmware/txm.iphoneos.release.im4p CFW/Firmware/txm.iphoneos.release.im4p.bak")
os.system("pyimg4 im4p extract -i CFW/Firmware/txm.iphoneos.release.im4p.bak -o TXM.raw")
# patch 
apply_patches("txm-boot", "TXM.raw")
#create im4p
os.system("pyimg4 im4p create -i TXM.raw -o TXM.im4p -d 1 -f trxm --lzfse")
# preserve payp structure
txm_im4p_data = Path('CFW/Firmware/txm.iphoneos.release.im4p.bak').read_bytes()
payp_offset = txm_im4p_data.rfind(b'PAYP')
if payp_offset == -1:
    print("Couldn't find payp structure !!!")
    sys.exit()

with open('TXM.im4p', 'ab') as f:
    f.write(txm_im4p_data[(payp_offset-10):])

payp_sz = len(txm_im4p_data[(payp_offset-10):])
print(f"payp sz: {payp_sz}")

txm_im4p_data = bytearray(open('TXM.im4p', 'rb').read())
txm_im4p_data[2:5] = (int.from_bytes(txm_im4p_data[2:5], 'big') + payp_sz).to_bytes(3, 'big')
open('TXM.im4p', 'wb').write(txm_im4p_data)

# sign
os.system("pyimg4 img4 create -p TXM.im4p -d 1 -o Ramdisk/TXM.img4 -m t8030_apticket.der")

# GFX
if not os.path.exists("CFW/Firmware/agx/armfw_g12p.im4p.bak"):
    os.system("cp CFW/Firmware/agx/armfw_g12p.im4p CFW/Firmware/agx/armfw_g12p.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/agx/armfw_g12p.im4p.bak -o Ramdisk/GFX.img4 -M t8030_apticket.der -T gfxf")

# ISP
if not os.path.exists("CFW/Firmware/isp_bni/adc-zelus-d79.im4p.bak"):
    os.system("cp CFW/Firmware/isp_bni/adc-zelus-d79.im4p CFW/Firmware/isp_bni/adc-zelus-d79.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/isp_bni/adc-zelus-d79.im4p.bak -o Ramdisk/ISP.img4 -M t8030_apticket.der -T ispf")

# PMP
if not os.path.exists("CFW/Firmware/pmp/t8030pmp.im4p.bak"):
    os.system("cp CFW/Firmware/pmp/t8030pmp.im4p CFW/Firmware/pmp/t8030pmp.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/pmp/t8030pmp.im4p.bak -o Ramdisk/PMP.img4 -M t8030_apticket.der -T pmpf")

# RestoreTrustCache
if not os.path.exists("CFW/Firmware/094-13753-162.dmg.trustcache.bak"):
    os.system("cp CFW/Firmware/094-13753-162.dmg.trustcache CFW/Firmware/094-13753-162.dmg.trustcache.bak")
os.system("../tools/img4 -i CFW/Firmware/094-13753-162.dmg.trustcache.bak -o Ramdisk/RestoreTrustCache.img4 -M t8030_apticket.der -T rtsc")

# SIO
if not os.path.exists("CFW/Firmware/SmartIOFirmware_ASCv2.im4p.bak"):
    os.system("cp CFW/Firmware/SmartIOFirmware_ASCv2.im4p CFW/Firmware/SmartIOFirmware_ASCv2.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/SmartIOFirmware_ASCv2.im4p.bak -o Ramdisk/SIO.img4 -M t8030_apticket.der -T siof")

# WCH
if not os.path.exists("CFW/Firmware/WirelessPower/WirelessPower.iphone12c.im4p.bak"):
    os.system("cp CFW/Firmware/WirelessPower/WirelessPower.iphone12c.im4p CFW/Firmware/WirelessPower/WirelessPower.iphone12c.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/WirelessPower/WirelessPower.iphone12c.im4p.bak -o Ramdisk/WCH.img4 -M t8030_apticket.der -T wchf")


# DeviceTree
if not os.path.exists("CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/DeviceTree.d79ap.im4p CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak -o DeviceTree.raw")
# Abort if the DeviceTree patcher fails. Upstream ignores the return code, so when
# patch_dt2.py was non-executable ("Permission denied") the build silently carried on and
# reused a stale DeviceTree_patched.raw left behind by get_rd.py, producing a DeviceTree
# with only 1 of the 3 required properties. The two missing ones are the AppleKeyStore
# SEP workarounds, and without them the kernel hangs initialising keybagd.
if os.system(f'{sys.executable} ./patch_dt2.py DeviceTree.raw -o DeviceTree_patched.raw') != 0:
    sys.exit("[!] patch_dt2.py failed, stopping.")
os.system("../tools/img4tool -c DeviceTree.im4p -t dtre DeviceTree_patched.raw")
os.system("../tools/img4 -i DeviceTree.im4p -o Ramdisk/DeviceTree.img4 -M t8030_apticket.der -T rdtr")

# SEP
if not os.path.exists("CFW/Firmware/all_flash/sep-firmware.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/sep-firmware.d79.RELEASE.im4p CFW/Firmware/all_flash/sep-firmware.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/sep-firmware.d79.RELEASE.im4p.bak -o Ramdisk/SEP.img4 -M t8030_apticket.der -T rsep")

# Kernelcache
if not os.path.exists("CFW/kernelcache.release.iphone12c.bak"):
    os.system("cp CFW/kernelcache.release.iphone12c CFW/kernelcache.release.iphone12c.bak")
os.system("pyimg4 im4p extract -i CFW/kernelcache.release.iphone12c.bak -o kcache.raw")
# patch 
apply_patches(KERNEL_PATCH_TABLE, "kcache.raw")

#create im4p
os.system("pyimg4 im4p create -i kcache.raw -o krnl.im4p -d KernelManagement_host-514 -f rkrn --lzfse")

# preserve payp structure
kernel_im4p_data = Path('CFW/kernelcache.release.iphone12c.bak').read_bytes()
payp_offset = kernel_im4p_data.rfind(b'PAYP')
if payp_offset == -1:
    print("Couldn't find payp structure !!!")
    sys.exit()

with open('krnl.im4p', 'ab') as f:
    f.write(kernel_im4p_data[(payp_offset-10):])

payp_sz = len(kernel_im4p_data[(payp_offset-10):])
print(f"payp sz: {payp_sz}")

kernel_im4p_data = bytearray(open('krnl.im4p', 'rb').read())
kernel_im4p_data[2:6] = (int.from_bytes(kernel_im4p_data[2:6], 'big') + payp_sz).to_bytes(4, 'big')
open('krnl.im4p', 'wb').write(kernel_im4p_data)

# sign
os.system("pyimg4 img4 create -p krnl.im4p -o Ramdisk/Kernelcache.img4 -m t8030_apticket.der")



# NOTE: About sep boot fail?
# init_data_protection: can't open '/private/preboot/59A79B92174AC2C86D2186C59241328966B7BEF699FEB41644661A479A509074D5EBDBDD27B5A0EBCB3805EFDF6EEA7E/usr/standalone/firmware/sep-firmware.img4', errno: No such file or directory(2)
# ramdisk 로 부팅해서 내장된 apticket.der을 가져온다.
# 그걸로 서명한 sep-firmware을 가져다놓는다. 끝?


# clean
os.system("rm TXM.im4p")
os.system("rm TXM.raw")
os.system("rm SPTM.img4")
os.system("rm RestoreTrustCache.img4")
os.system("rm RestoreLogo.raw")
os.system("rm RestoreLogo.im4p")
os.system("rm PMP.img4")
os.system("rm krnl.im4p")
os.system("rm kcache.raw")
os.system("rm ISP.img4")
os.system("rm iBEC.raw")
os.system("rm iBEC.im4p")
os.system("rm GXF.img4")
os.system("rm AVE.raw")
os.system("rm AVE.im4p")
os.system("rm AOP.im4p")
os.system("rm AOP.raw")
os.system("rm ANE.im4p")
os.system("rm ANE.raw")
os.system("rm -rf ramdisk.dmg")
os.system("rm -rf ramdisk1.dmg")
os.system("rm -rf ramdisk1.dmg.im4p")
os.system("rm -rf DeviceTree.im4p")
os.system("rm -rf SSHRD")
