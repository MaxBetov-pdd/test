#!/bin/sh
set -e
cd "$(dirname "$0")"
LDID=../../tools/ldid_macosx_arm64
xcrun -sdk iphoneos clang -arch arm64 -O2 -Wall -framework Foundation photodiag.m -o photodiag
"$LDID" -Icom.apple.photodiag -Sphotodiag.ent -Cadhoc photodiag
GOT=$(codesign -dv photodiag 2>&1 | sed -n 's/^Identifier=//p')
[ "$GOT" = "com.apple.photodiag" ] || { echo "[!] identifier is '$GOT'"; exit 1; }
codesign -d --entitlements :- photodiag 2>/dev/null | grep -q get-task-allow && { echo "[!] get-task-allow present"; exit 1; }
echo "[+] photodiag  $(wc -c < photodiag | tr -d ' ') bytes, $(file -b photodiag)"
echo "[+] identifier $GOT, entitlements $(codesign -d --entitlements :- photodiag 2>/dev/null | grep -o '<key>' | wc -l | tr -d ' ')"
