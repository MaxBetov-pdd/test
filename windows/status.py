#!/usr/bin/env python3
"""Show an A12/A13 device connected in DFU or Recovery mode on Windows."""

from __future__ import annotations

import sys

from ich_usb import IchUsbError, format_query, query_device


def main() -> int:
    try:
        info = query_device()
    except IchUsbError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if info is None:
        print("No Apple DFU/Recovery device found.")
        return 2
    print(format_query(info))
    if info.cpid not in {0x8020, 0x8027, 0x8030}:
        print("SUPPORT: unsupported CPID")
        return 3
    print("SUPPORT: A12/A13 toolkit OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
