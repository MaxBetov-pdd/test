#!/usr/bin/env python3.14
import struct
import os
import sys
import glob
import shutil
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

if sys.platform != "darwin":
    sys.exit("[!] get_rd.py needs macOS hdiutil to rebuild the APFS ramdisk. "
             "Use the build-d79-sshrd GitHub Actions workflow from Windows/Linux.")
if not Path("t8030_apticket.der").is_file():
    sys.exit("[!] missing t8030_apticket.der (IM4M used to wrap the boot components)")

GTAR = shutil.which("gtar") or str(Path("../tools/gtar"))


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
apply_patches("ibss-ramdisk", "Ramdisk/iBSS.raw")

# 2. Grab & Patch iBEC
if not os.path.exists("CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak"):
    os.system("cp CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/dfu/iBEC.d79.RELEASE.im4p.bak -o iBEC.raw")
apply_patches("ibss-ramdisk", "iBEC.raw")
os.system("../tools/img4tool -c iBEC.im4p -t ibec iBEC.raw")
os.system("../tools/img4 -i iBEC.im4p -o Ramdisk/iBEC.img4 -M t8030_apticket.der")

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
# Upstream has the next line indented into the block above, so AVE.img4 is only built when
# the .bak does not yet exist. Every other component builds it unconditionally, and this
# script starts with `rm -rf Ramdisk`, so on any re-run AVE.img4 would go missing.
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
apply_patches("txm-restore", "TXM.raw")
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

# RestoreRamdisk
if not os.path.exists("CFW/094-13753-162.dmg.bak"):
    os.system("cp CFW/094-13753-162.dmg CFW/094-13753-162.dmg.bak")
# 8. Grab ramdisk & build custom ramdisk
os.system("pyimg4 im4p extract -i CFW/094-13753-162.dmg.bak -o ramdisk.dmg")
# 
os.system("mkdir SSHRD")
os.system("sudo hdiutil attach -mountpoint SSHRD ramdisk.dmg -owners off")
os.system("sudo hdiutil create -size 254m -imagekey diskimage-class=CRawDiskImage -format UDZO -fs APFS -layout NONE -srcfolder SSHRD -copyuid root ramdisk1.dmg")
os.system("sudo hdiutil detach -force SSHRD")
os.system("sudo hdiutil attach -mountpoint SSHRD ramdisk1.dmg -owners off")
# sys.stdin.read(1)
#remove unneccessary files for expand space
os.system(f'sudo "{GTAR}" -x --no-overwrite-dir -f ssh.tar.gz -C SSHRD/')
os.system("rm SSHRD/usr/bin/img4tool")
os.system("rm SSHRD/usr/bin/img4")
os.system("rm SSHRD/usr/sbin/dietappleh13camerad")
os.system("rm SSHRD/usr/sbin/dietappleh16camerad")
os.system("rm SSHRD/usr/local/bin/wget")
os.system("rm SSHRD/usr/local/bin/procexp")
# Fix sftp-server not working
os.system(f"../tools/ldid_macosx_arm64 -Ssftp_server_ents.plist -M -Cadhoc SSHRD/usr/libexec/sftp-server")
os.system("sudo hdiutil detach -force SSHRD")
os.system("sudo hdiutil resize -sectors min ramdisk1.dmg")
# sign
os.system("pyimg4 im4p create -i ramdisk1.dmg -o ramdisk1.dmg.im4p -f rdsk")
os.system("pyimg4 img4 create -p ramdisk1.dmg.im4p -o Ramdisk/RestoreRamdisk.img4 -m t8030_apticket.der")

# DeviceTree
if not os.path.exists("CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak"):
    os.system("cp CFW/Firmware/all_flash/DeviceTree.d79ap.im4p CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak")
os.system("../tools/img4 -i CFW/Firmware/all_flash/DeviceTree.d79ap.im4p.bak -o DeviceTree.raw")
# Abort if the patcher fails; upstream ignores the return code and would silently reuse
# a stale DeviceTree_patched.raw from an earlier run. Invoked via sys.executable so a
# missing execute bit on the script cannot break the build.
if os.system(f'{sys.executable} ./patch_dt.py DeviceTree.raw -o DeviceTree_patched.raw') != 0:
    sys.exit("[!] patch_dt.py failed, stopping.")
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
apply_patches("kc-restore", "kcache.raw")

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

# clean
os.system("rm TXM.im4p")
os.system("rm TXM.raw")
os.system("rm SPTM.img4")
os.system("rm RestoreTrustCache.img4")
os.system("rm RestoreLogo.raw")
os.system("rm RestoreLogo.im4p")
os.system("rm PMP.img4")
os.system("rm krnl.im4p")
# os.system("rm kcache.raw")
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
