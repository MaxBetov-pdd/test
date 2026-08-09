from __future__ import annotations

import contextlib
import io
import plistlib
import socket
import socketserver
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


WINDOWS_DIR = Path(__file__).resolve().parents[1]
ROOT = WINDOWS_DIR.parent
PATCH_DIR = ROOT / "patch"
sys.path.insert(0, str(WINDOWS_DIR))
sys.path.insert(0, str(PATCH_DIR))

import build  # noqa: E402
import boot  # noqa: E402
import finalize_iboot  # noqa: E402
import ich_usb  # noqa: E402
import iproxy  # noqa: E402
import preflight  # noqa: E402
from iboot_patchfinder import encode_add_imm, encode_adrp  # noqa: E402
from img4tools import extract_im4p, wrap_existing_im4p, wrap_raw  # noqa: E402
from pyimg4 import IMG4, IM4P  # noqa: E402
from trustcache import CdHash, append_hashes  # noqa: E402


class DfuProtocolTests(unittest.TestCase):
    def test_direct_boot_uses_exact_usbliter8_sequence(self) -> None:
        class FakeUsbError(Exception):
            pass

        class FakeCore:
            USBError = FakeUsbError

        class FakeDevice:
            def __init__(self) -> None:
                self.calls: list[tuple[int, int, int, int, bytes, int]] = []

            def ctrl_transfer(self, request_type, request, value, index, data, timeout):
                payload = bytes(data)
                self.calls.append((request_type, request, value, index, payload, timeout))
                return len(payload)

        device = FakeDevice()
        payload = bytes(range(256)) * 17
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "ibec.bin"
            image.write_bytes(payload)
            with (
                mock.patch.object(ich_usb, "_imports", return_value=(FakeCore, object())),
                mock.patch.object(ich_usb, "wait_for_raw", return_value=device),
                mock.patch.object(ich_usb, "_serial_for", return_value="CPID:8020 PWND:[usbliter8]"),
                mock.patch.object(ich_usb, "_claim"),
                mock.patch.object(ich_usb, "_release"),
            ):
                ich_usb.direct_pwned_dfu_boot(image)

        uploads = device.calls[:-3]
        self.assertEqual([len(call[4]) for call in uploads], [0x800, 0x800, 0x100])
        self.assertTrue(all(call[:4] == (0x21, 1, 0, 0) for call in uploads))
        self.assertEqual(
            [(call[0], call[1], call[2], call[3], call[4]) for call in device.calls[-3:]],
            [(0x21, 1, 0, 0, b""), (0x21, 8, 0, 0, b""), (0x21, 6, 0, 0, b"")],
        )


class D79WrapperTests(unittest.TestCase):
    @staticmethod
    def _set_reference(data: bytearray, off: int, target: int, register: int) -> None:
        base = finalize_iboot.D79_IBOOT_BASE
        struct.pack_into(
            "<I",
            data,
            off,
            encode_adrp(register, base + off, (base + target) & ~0xFFF),
        )
        struct.pack_into(
            "<I",
            data,
            off + 4,
            encode_add_imm(register, register, target & 0xFFF),
        )

    def _images(self) -> tuple[bytearray, bytearray, int]:
        stock = bytearray(0x100000)
        stock[0xF1000 : 0xF1000 + len(finalize_iboot.D79_EXPECTED_BUILD_TAG)] = (
            finalize_iboot.D79_EXPECTED_BUILD_TAG
        )
        stock[0xF1040 : 0xF1040 + len(finalize_iboot.D79_EXPECTED_BUILD_TAG)] = (
            finalize_iboot.D79_EXPECTED_BUILD_TAG
        )
        branch = finalize_iboot.D79_IMAGE4_CANARY_BRANCH
        struct.pack_into("<I", stock, branch - 4, 0x94000000)  # BL
        struct.pack_into("<I", stock, branch, 0x54000001)  # B.NE
        struct.pack_into("<I", stock, branch + 4, 0xAA0103E0)  # MOV X0, X1

        original_slot = 0xF0000
        boot_args_slot = 0xB0000
        stock[original_slot : original_slot + 3] = b"%s\0"
        stock[boot_args_slot : boot_args_slot + len(finalize_iboot.RAMDISK_BOOT_ARGS)] = (
            finalize_iboot.RAMDISK_BOOT_ARGS
        )
        references = (
            finalize_iboot.D79_BOOT_ARGS_REFERENCE,
            *finalize_iboot.D79_FALSE_BOOT_ARGS_REFERENCES,
        )
        for off, register in zip(references, (2, 1, 2, 2)):
            self._set_reference(stock, off, original_slot, register)

        patched = bytearray(stock)
        patched[branch : branch + 4] = finalize_iboot.NOP
        patched[branch + 4 : branch + 8] = finalize_iboot.MOV_X0_ZERO
        for off, register in zip(references, (2, 1, 2, 2)):
            self._set_reference(patched, off, boot_args_slot, register)
        return stock, patched, boot_args_slot

    def test_d79_wrapper_restores_only_unrelated_shared_string_references(self) -> None:
        stock, patched, boot_args_slot = self._images()
        finalize_iboot.apply_d79_wrapper(stock, patched, boot_args_slot)

        self.assertNotEqual(
            patched[
                finalize_iboot.D79_BOOT_ARGS_REFERENCE :
                finalize_iboot.D79_BOOT_ARGS_REFERENCE + 8
            ],
            stock[
                finalize_iboot.D79_BOOT_ARGS_REFERENCE :
                finalize_iboot.D79_BOOT_ARGS_REFERENCE + 8
            ],
        )
        for off in finalize_iboot.D79_FALSE_BOOT_ARGS_REFERENCES:
            self.assertEqual(patched[off : off + 8], stock[off : off + 8])

    def test_d79_wrapper_fails_closed_on_unconfirmed_canary(self) -> None:
        stock, patched, boot_args_slot = self._images()
        struct.pack_into("<I", stock, finalize_iboot.D79_IMAGE4_CANARY_BRANCH, 0)
        with self.assertRaisesRegex(SystemExit, "expected B.NE"):
            finalize_iboot.apply_d79_wrapper(stock, patched, boot_args_slot)

    def test_preflight_rejects_active_kernel_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bootchain = Path(temp)
            (bootchain / "kernelcache.img4").write_bytes(b"stock")
            (bootchain / "kernelcache.img4.patched").write_bytes(b"patched")
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    preflight.validate_selected_kernel(bootchain, "patched")


class TargetIdentityTests(unittest.TestCase):
    def test_live_target_accepts_exact_board_and_ecid(self) -> None:
        info = SimpleNamespace(board="d79ap", ecid="00094D3A3A6B802E")
        args = SimpleNamespace(expected_board="d79ap", expected_ecid="0x94d3a3a6b802e")
        boot.require_expected_target(info, args)

    def test_live_target_rejects_other_board(self) -> None:
        info = SimpleNamespace(board="n104ap", ecid="00094D3A3A6B802E")
        args = SimpleNamespace(expected_board="d79ap", expected_ecid="")
        with self.assertRaisesRegex(ich_usb.IchUsbError, "expected d79ap"):
            boot.require_expected_target(info, args)

    def test_live_target_rejects_other_ecid(self) -> None:
        info = SimpleNamespace(board="d79ap", ecid="1")
        args = SimpleNamespace(expected_board="d79ap", expected_ecid="2")
        with self.assertRaisesRegex(ich_usb.IchUsbError, "expected 2"):
            boot.require_expected_target(info, args)


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        chunk = sock.recv(size - len(result))
        if not chunk:
            raise RuntimeError("test usbmux client disconnected")
        result.extend(chunk)
    return bytes(result)


def _recv_mux_request(sock: socket.socket) -> dict:
    header = _recv_exact(sock, iproxy.HEADER.size)
    length, _version, _message, _tag = iproxy.HEADER.unpack(header)
    return plistlib.loads(_recv_exact(sock, length - iproxy.HEADER.size))


def _send_mux_response(sock: socket.socket, payload: dict) -> None:
    body = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
    sock.sendall(
        iproxy.HEADER.pack(
            iproxy.HEADER.size + len(body),
            iproxy.PLIST_VERSION,
            iproxy.PLIST_MESSAGE,
            1,
        )
        + body
    )


class _MuxHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        request = _recv_mux_request(self.request)
        server.requests.append(request)
        if request["MessageType"] == "ListDevices":
            _send_mux_response(
                self.request,
                {
                    "DeviceList": [
                        {
                            "DeviceID": 7,
                            "Properties": {
                                "SerialNumber": "TEST-UDID",
                                "ConnectionType": "USB",
                            },
                        }
                    ]
                },
            )
        elif request["MessageType"] == "Connect":
            _send_mux_response(self.request, {"Number": 0})


class UsbmuxTests(unittest.TestCase):
    def test_connect_uses_device_id_and_network_order_port(self) -> None:
        with socketserver.ThreadingTCPServer(("127.0.0.1", 0), _MuxHandler) as server:
            server.requests = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address
            connection = iproxy.connect_device(22, host=host, port=port)
            connection.close()
            server.shutdown()
            thread.join(timeout=2)

        self.assertEqual([item["MessageType"] for item in server.requests], ["ListDevices", "Connect"])
        self.assertEqual(server.requests[1]["DeviceID"], 7)
        self.assertEqual(server.requests[1]["PortNumber"], socket.htons(22))


class Img4Tests(unittest.TestCase):
    def test_synthetic_im4p_and_img4_round_trip(self) -> None:
        ticket = ROOT / "resources" / "IM4M_0x8020"
        payload = b"ICH-WINDOWS-ROUNDTRIP" * 32
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            wrapped = temp_path / "payload.img4"
            wrap_raw(payload, wrapped, ticket, fourcc="test", description="unit test")
            self.assertEqual(extract_im4p(wrapped.read_bytes()), payload)
            parsed = IMG4(wrapped.read_bytes())
            self.assertEqual(parsed.im4p.fourcc, "test")

            source = temp_path / "source.im4p"
            source.write_bytes(IM4P(fourcc="dtre", description="tree", payload=payload).output())
            rewrapped = temp_path / "tree.img4"
            wrap_existing_im4p(source, rewrapped, ticket, fourcc="rdtr")
            self.assertEqual(IMG4(rewrapped.read_bytes()).im4p.fourcc, "rdtr")
            self.assertEqual(extract_im4p(rewrapped.read_bytes()), payload)


class TrustCacheTests(unittest.TestCase):
    def test_static_entry_formats(self) -> None:
        item = CdHash(b"\xA5" * 20, 2)
        for version, entry_size in ((0, 20), (1, 22), (2, 24)):
            with self.subTest(version=version):
                header = bytearray(24)
                struct.pack_into("<I", header, 0, version)
                struct.pack_into("<I", header, 20, 0)
                output = append_hashes(bytes(header), [item, item])
                self.assertEqual(struct.unpack_from("<I", output, 20)[0], 1)
                self.assertEqual(len(output), 24 + entry_size)
                self.assertEqual(output[24:44], item.value)
                if entry_size >= 22:
                    self.assertEqual(output[44], item.hash_type)


class ManifestTests(unittest.TestCase):
    def test_component_paths_selects_board_and_aliases(self) -> None:
        required = {
            "iBEC": "Firmware/ibec.im4p",
            "DeviceTree": "Firmware/tree.im4p",
            "KernelCache": "kernelcache",
            "RestoreRamDisk": "ramdisk.dmg",
            "RestoreTrustCache": "ramdisk.trustcache",
        }
        images = {key: {"Info": {"Path": path}} for key, path in required.items()}
        images["Ap,SPTM"] = {"Info": {"Path": "Firmware/sptm.im4p"}}
        manifest = {
            "BuildIdentities": [
                {
                    "Info": {"DeviceClass": "n841ap", "BuildNumber": "22H355"},
                    "Manifest": images,
                }
            ]
        }
        paths, build_number = build.component_paths(plistlib.dumps(manifest), "n841ap")
        self.assertEqual(build_number, "22H355")
        self.assertEqual(paths["iBEC"], "Firmware/ibec.im4p")
        self.assertEqual(paths["SPTM"], "Firmware/sptm.im4p")

    def test_prepared_ramdisk_must_be_expanded(self) -> None:
        data = bytearray(1024)
        data[32:36] = b"NXSB"
        with self.assertRaisesRegex(build.BuildError, "at least 256 MiB"):
            build.validate_ramdisk(bytes(data), Path("small.dmg"), prepared=True)


if __name__ == "__main__":
    unittest.main()
