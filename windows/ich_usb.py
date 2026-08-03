"""Small Windows USB transport for ICH A12/A13 ramdisk.

This module replaces the subset of ``irecovery`` and ``usbliter8_boot`` used
by the project.  It intentionally talks only to Apple's DFU/Recovery USB
interfaces and never writes to a normal-mode iOS device.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


APPLE_VENDOR_ID = 0x05AC
DFU_PRODUCT_ID = 0x1227
RECOVERY_PRODUCT_IDS = frozenset({0x1280, 0x1281, 0x1282, 0x1283})
SUPPORTED_CPIDS = frozenset({0x8020, 0x8027, 0x8030})


class IchUsbError(RuntimeError):
    """A USB transport or device-state error."""


class IchUsbTimeout(IchUsbError):
    """The requested USB mode did not appear before the timeout."""


@dataclass(frozen=True)
class DeviceRecord:
    product: str
    board: str
    name: str


@dataclass(frozen=True)
class DeviceInfo:
    mode: str
    product_id: int
    serial: str
    fields: dict[str, str]
    product: str
    board: str
    name: str

    @property
    def cpid(self) -> int | None:
        return _hex_field(self.fields, "CPID")

    @property
    def bdid(self) -> int | None:
        return _hex_field(self.fields, "BDID")

    @property
    def ecid(self) -> str:
        return self.fields.get("ECID", "")

    @property
    def pwned(self) -> str:
        return self.fields.get("PWND", "")


# Facts mirrored by libirecovery's public device database.  The port keeps the
# table local so status/build do not depend on a large third-party framework.
DEVICE_DB: dict[tuple[int, int], DeviceRecord] = {
    # A12 iPhone
    (0x8020, 0x0E): DeviceRecord("iPhone11,2", "d321ap", "iPhone XS"),
    (0x8020, 0x0A): DeviceRecord("iPhone11,4", "d331ap", "iPhone XS Max (China)"),
    (0x8020, 0x1A): DeviceRecord("iPhone11,6", "d331pap", "iPhone XS Max"),
    (0x8020, 0x0C): DeviceRecord("iPhone11,8", "n841ap", "iPhone XR"),
    # A13 iPhone
    (0x8030, 0x04): DeviceRecord("iPhone12,1", "n104ap", "iPhone 11"),
    (0x8030, 0x06): DeviceRecord("iPhone12,3", "d421ap", "iPhone 11 Pro"),
    (0x8030, 0x02): DeviceRecord("iPhone12,5", "d431ap", "iPhone 11 Pro Max"),
    (0x8030, 0x10): DeviceRecord("iPhone12,8", "d79ap", "iPhone SE (2nd gen)"),
    # A12 iPad
    (0x8020, 0x14): DeviceRecord("iPad11,1", "j210ap", "iPad mini (5th gen, WiFi)"),
    (0x8020, 0x16): DeviceRecord("iPad11,2", "j211ap", "iPad mini (5th gen, Cellular)"),
    (0x8020, 0x1C): DeviceRecord("iPad11,3", "j217ap", "iPad Air (3rd gen, WiFi)"),
    (0x8020, 0x1E): DeviceRecord("iPad11,4", "j218ap", "iPad Air (3rd gen, Cellular)"),
    (0x8020, 0x24): DeviceRecord("iPad11,6", "j171aap", "iPad (8th gen, WiFi)"),
    (0x8020, 0x26): DeviceRecord("iPad11,7", "j172aap", "iPad (8th gen, Cellular)"),
    # A13 iPad
    (0x8030, 0x18): DeviceRecord("iPad12,1", "j181ap", "iPad (9th gen, WiFi)"),
    (0x8030, 0x1A): DeviceRecord("iPad12,2", "j182ap", "iPad (9th gen, Cellular)"),
    # A12X/Z iPad Pro (kept because the original project accepts CPID 0x8027)
    (0x8027, 0x0C): DeviceRecord("iPad8,1", "j317ap", "iPad Pro 11-inch (1st gen, WiFi)"),
    (0x8027, 0x1C): DeviceRecord("iPad8,2", "j317xap", "iPad Pro 11-inch (1st gen, WiFi, 1TB)"),
    (0x8027, 0x0E): DeviceRecord("iPad8,3", "j318ap", "iPad Pro 11-inch (1st gen, Cellular)"),
    (0x8027, 0x1E): DeviceRecord("iPad8,4", "j318xap", "iPad Pro 11-inch (1st gen, Cellular, 1TB)"),
    (0x8027, 0x08): DeviceRecord("iPad8,5", "j320ap", "iPad Pro 12.9-inch (3rd gen, WiFi)"),
    (0x8027, 0x18): DeviceRecord("iPad8,6", "j320xap", "iPad Pro 12.9-inch (3rd gen, WiFi, 1TB)"),
    (0x8027, 0x0A): DeviceRecord("iPad8,7", "j321ap", "iPad Pro 12.9-inch (3rd gen, Cellular)"),
    (0x8027, 0x1A): DeviceRecord("iPad8,8", "j321xap", "iPad Pro 12.9-inch (3rd gen, Cellular, 1TB)"),
    (0x8027, 0x3C): DeviceRecord("iPad8,9", "j417ap", "iPad Pro 11-inch (2nd gen, WiFi)"),
    (0x8027, 0x3E): DeviceRecord("iPad8,10", "j418ap", "iPad Pro 11-inch (2nd gen, Cellular)"),
    (0x8027, 0x38): DeviceRecord("iPad8,11", "j420ap", "iPad Pro 12.9-inch (4th gen, WiFi)"),
    (0x8027, 0x3A): DeviceRecord("iPad8,12", "j421ap", "iPad Pro 12.9-inch (4th gen, Cellular)"),
}


def _imports():
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise IchUsbError(
            "PyUSB is not installed. Run: powershell -ExecutionPolicy Bypass -File .\\windows\\setup.ps1"
        ) from exc
    return usb.core, usb.util


def _backend():
    usb_core, _ = _imports()
    try:
        import libusb_package

        backend = libusb_package.get_libusb1_backend()
    except ImportError:
        from usb.backend import libusb1

        backend = libusb1.get_backend()
    if backend is None:
        raise IchUsbError(
            "No libusb backend was found. Re-run windows/setup.ps1 inside the project."
        )
    # Keep a reference to usb.core so frozen/static analysers do not treat the
    # import as unused; more importantly, this validates the loaded backend.
    _ = usb_core
    return backend


def _hex_field(fields: dict[str, str], key: str) -> int | None:
    value = fields.get(key)
    if not value:
        return None
    try:
        return int(value.removeprefix("0x"), 16)
    except ValueError:
        return None


def parse_serial(serial: str) -> dict[str, str]:
    """Parse iBoot's ``KEY:value`` and ``KEY:[value]`` serial fields."""

    fields: dict[str, str] = {}
    pattern = re.compile(r"(?:^|\s)([A-Z0-9-]{3,16}):(?:\[([^\]]*)\]|([^\s]+))")
    for match in pattern.finditer(serial):
        fields[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return fields


def _mode_for_pid(product_id: int) -> str:
    if product_id == DFU_PRODUCT_ID:
        return "DFU"
    if product_id in RECOVERY_PRODUCT_IDS:
        return "Recovery"
    return f"Unknown(0x{product_id:04X})"


def _find_raw(product_ids: Iterable[int] | None = None):
    usb_core, _ = _imports()
    wanted = set(product_ids) if product_ids is not None else None
    try:
        devices = usb_core.find(
            find_all=True,
            idVendor=APPLE_VENDOR_ID,
            backend=_backend(),
        )
        for device in devices or ():
            if wanted is None or int(device.idProduct) in wanted:
                return device
    except usb_core.NoBackendError as exc:
        raise IchUsbError("libusb could not be loaded") from exc
    except usb_core.USBError as exc:
        raise _friendly_usb_error(exc) from exc
    return None


def _friendly_usb_error(exc: BaseException) -> IchUsbError:
    text = str(exc)
    lower = text.lower()
    if "access" in lower or "permission" in lower or "busy" in lower:
        return IchUsbError(
            f"Windows USB driver denied access ({text}). Install WinUSB for the current Apple "
            "DFU/Recovery device with Zadig, then reconnect it."
        )
    return IchUsbError(f"USB error: {text}")


def wait_for_raw(product_ids: Iterable[int], timeout: float):
    deadline = time.monotonic() + timeout
    wanted = set(product_ids)
    while time.monotonic() < deadline:
        device = _find_raw(wanted)
        if device is not None:
            return device
        time.sleep(0.25)
    labels = ", ".join(f"0x{x:04X}" for x in sorted(wanted))
    raise IchUsbTimeout(f"Timed out waiting for Apple USB mode(s): {labels}")


def _serial_for(device) -> str:
    _, usb_util = _imports()
    try:
        index = int(getattr(device, "iSerialNumber", 0) or 0)
        return str(usb_util.get_string(device, index) or "") if index else ""
    except Exception as exc:  # PyUSB backends expose several platform-specific errors.
        raise _friendly_usb_error(exc) from exc


def query_device(timeout: float = 0) -> DeviceInfo | None:
    product_ids = {DFU_PRODUCT_ID, *RECOVERY_PRODUCT_IDS}
    device = wait_for_raw(product_ids, timeout) if timeout > 0 else _find_raw(product_ids)
    if device is None:
        return None
    serial = _serial_for(device)
    fields = parse_serial(serial)
    cpid = _hex_field(fields, "CPID")
    bdid = _hex_field(fields, "BDID")
    record = DEVICE_DB.get((cpid, bdid)) if cpid is not None and bdid is not None else None
    return DeviceInfo(
        mode=_mode_for_pid(int(device.idProduct)),
        product_id=int(device.idProduct),
        serial=serial,
        fields=fields,
        product=record.product if record else "unknown",
        board=record.board if record else "unknown",
        name=record.name if record else "unknown",
    )


def _claim(device, interface: int = 0) -> None:
    usb_core, usb_util = _imports()
    try:
        try:
            device.set_configuration()
        except usb_core.USBError:
            # An already-configured Apple interface commonly returns BUSY here.
            device.get_active_configuration()
        try:
            if device.is_kernel_driver_active(interface):
                device.detach_kernel_driver(interface)
        except (NotImplementedError, usb_core.USBError):
            pass
        usb_util.claim_interface(device, interface)
        try:
            device.set_interface_altsetting(interface=interface, alternate_setting=0)
        except usb_core.USBError:
            pass
    except Exception as exc:
        raise _friendly_usb_error(exc) from exc


def _release(device, interface: int = 0) -> None:
    _, usb_util = _imports()
    try:
        usb_util.release_interface(device, interface)
    except Exception:
        pass
    usb_util.dispose_resources(device)


def direct_pwned_dfu_boot(
    image_path: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    warning: Callable[[str], None] | None = None,
) -> None:
    """Upload a raw patched iBSS/iBEC using the bundled tool's exact protocol.

    The sequence was recovered from both architectures of ``usbliter8_boot``:
    0x800-byte DFU_DNLOAD requests with block number zero, followed by requests
    1 (zero length), 8 and 6.  Request 6 triggers the jump/disconnect.
    """

    usb_core, _ = _imports()
    data = Path(image_path).read_bytes()
    if not data:
        raise IchUsbError(f"Boot image is empty: {image_path}")
    device = wait_for_raw({DFU_PRODUCT_ID}, 8)
    serial = _serial_for(device)
    fields = parse_serial(serial)
    if fields.get("PWND", "").lower() != "usbliter8":
        raise IchUsbError(
            f"Device is in DFU but not pwned by usbliter8 (PWND={fields.get('PWND', 'none')})"
        )
    _claim(device)
    try:
        total = len(data)
        for offset in range(0, total, 0x800):
            chunk = data[offset : offset + 0x800]
            try:
                sent = int(
                    device.ctrl_transfer(
                        0x21,
                        1,
                        0,
                        0,
                        chunk,
                        timeout=5000,
                    )
                )
            except usb_core.USBError as exc:
                raise IchUsbError(f"DFU_DNLOAD failed at 0x{offset:X}: {exc}") from exc
            if sent != len(chunk):
                raise IchUsbError(
                    f"Short DFU_DNLOAD at 0x{offset:X}: sent {sent} of {len(chunk)} bytes"
                )
            if progress:
                progress(min(offset + len(chunk), total), total)

        for request in (1, 8, 6):
            try:
                device.ctrl_transfer(0x21, request, 0, 0, b"", timeout=1000)
            except usb_core.USBError as exc:
                # The device is expected to disappear during this sequence.
                if warning and request != 6:
                    warning(f"DFU closing request {request} returned: {exc}")
    finally:
        _release(device)


class RecoveryClient:
    """Subset of irecovery used by boot.sh: upload, command and getenv."""

    def __init__(self, timeout: float = 8):
        self._usb_core, _ = _imports()
        self.device = wait_for_raw(RECOVERY_PRODUCT_IDS, timeout)
        _claim(self.device)

    def close(self) -> None:
        if self.device is not None:
            _release(self.device)
            self.device = None

    def __enter__(self) -> "RecoveryClient":
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def send_command(self, command: str, timeout_ms: int = 30_000) -> None:
        if self.device is None:
            raise IchUsbError("Recovery connection is closed")
        payload = command.encode("utf-8") + b"\0"
        try:
            self.device.ctrl_transfer(0x40, 0, 0, 0, payload, timeout=timeout_ms)
        except self._usb_core.USBError as exc:
            raise IchUsbError(f"Recovery command failed ({command!r}): {exc}") from exc

    def getenv(self, name: str, timeout_ms: int = 10_000) -> str:
        self.send_command(f"getenv {name}", timeout_ms=timeout_ms)
        try:
            data = bytes(self.device.ctrl_transfer(0xC0, 0, 0, 0, 255, timeout=timeout_ms))
        except self._usb_core.USBError as exc:
            raise IchUsbError(f"getenv {name!r} failed: {exc}") from exc
        return data.rstrip(b"\0").decode("utf-8", errors="replace")

    def send_file(
        self,
        path: Path,
        *,
        timeout_ms: int = 300_000,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if self.device is None:
            raise IchUsbError("Recovery connection is closed")
        data = Path(path).read_bytes()
        if not data:
            raise IchUsbError(f"Upload file is empty: {path}")
        try:
            self.device.ctrl_transfer(0x41, 0, 0, 0, b"", timeout=10_000)
            total = len(data)
            for offset in range(0, total, 0x8000):
                chunk = data[offset : offset + 0x8000]
                sent = int(self.device.write(0x04, chunk, timeout=timeout_ms))
                if sent != len(chunk):
                    raise IchUsbError(
                        f"Short Recovery upload at 0x{offset:X}: sent {sent} of {len(chunk)} bytes"
                    )
                if progress:
                    progress(min(offset + sent, total), total)
            if total % 512 == 0:
                self.device.write(0x04, b"", timeout=10_000)
        except IchUsbError:
            raise
        except self._usb_core.USBError as exc:
            raise IchUsbError(f"Recovery upload failed for {path.name}: {exc}") from exc


def format_query(info: DeviceInfo) -> str:
    cpid = info.cpid
    chip = {0x8020: "A12", 0x8027: "A12X/Z", 0x8030: "A13"}.get(cpid, "unknown")
    lines = [
        f"MODE: {info.mode}",
        f"PRODUCT: {info.product}",
        f"MODEL: {info.board}",
        f"NAME: {info.name}",
        f"CPID: {f'0x{cpid:04X}' if cpid is not None else 'unknown'}",
        f"CHIP: {chip}",
        f"ECID: {info.ecid or 'unknown'}",
        f"PWND: {info.pwned or 'none'}",
    ]
    return "\n".join(lines)
