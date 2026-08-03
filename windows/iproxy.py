#!/usr/bin/env python3
"""Minimal iproxy replacement for Apple Mobile Device Service on Windows."""

from __future__ import annotations

import argparse
import plistlib
import socket
import socketserver
import struct
import sys
import threading
from dataclasses import dataclass
from typing import Any


USBMUX_HOST = "127.0.0.1"
USBMUX_PORT = 27015
PLIST_VERSION = 1
PLIST_MESSAGE = 8
HEADER = struct.Struct("<IIII")


class UsbmuxError(RuntimeError):
    pass


@dataclass(frozen=True)
class MuxDevice:
    device_id: int
    serial: str
    connection_type: str


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise UsbmuxError("Apple Mobile Device Service closed the connection")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_plist(sock: socket.socket, payload: dict[str, Any], tag: int = 1) -> None:
    body = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
    sock.sendall(HEADER.pack(HEADER.size + len(body), PLIST_VERSION, PLIST_MESSAGE, tag) + body)


def _recv_plist(sock: socket.socket) -> dict[str, Any]:
    raw_header = _recv_exact(sock, HEADER.size)
    length, _version, message, _tag = HEADER.unpack(raw_header)
    if length < HEADER.size or length > 16 * 1024 * 1024:
        raise UsbmuxError(f"Invalid usbmux packet size: {length}")
    body = _recv_exact(sock, length - HEADER.size)
    if message != PLIST_MESSAGE:
        raise UsbmuxError(f"Unsupported usbmux response type: {message}")
    try:
        result = plistlib.loads(body)
    except Exception as exc:
        raise UsbmuxError("Apple Mobile Device Service returned an invalid plist") from exc
    if not isinstance(result, dict):
        raise UsbmuxError("Apple Mobile Device Service returned an unexpected response")
    return result


def _open_mux(host: str, port: int) -> socket.socket:
    try:
        return socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        raise UsbmuxError(
            f"Cannot connect to Apple Mobile Device Service at {host}:{port}. "
            "Install Apple Devices (or iTunes) and ensure its service is running."
        ) from exc


def list_devices(host: str = USBMUX_HOST, port: int = USBMUX_PORT) -> list[MuxDevice]:
    with _open_mux(host, port) as sock:
        _send_plist(
            sock,
            {
                "MessageType": "ListDevices",
                "ClientVersionString": "ICH Windows 1.0",
                "ProgName": "ich-iproxy",
                "kLibUSBMuxVersion": 3,
            },
        )
        response = _recv_plist(sock)
    devices: list[MuxDevice] = []
    for item in response.get("DeviceList", []):
        if not isinstance(item, dict):
            continue
        props = item.get("Properties", {})
        if not isinstance(props, dict):
            props = {}
        device_id = int(item.get("DeviceID", props.get("DeviceID", 0)) or 0)
        serial = str(props.get("SerialNumber", ""))
        connection_type = str(props.get("ConnectionType", "USB"))
        if device_id:
            devices.append(MuxDevice(device_id, serial, connection_type))
    return devices


def connect_device(
    remote_port: int,
    *,
    serial: str | None = None,
    host: str = USBMUX_HOST,
    port: int = USBMUX_PORT,
) -> socket.socket:
    devices = list_devices(host, port)
    if serial:
        normalized = serial.replace("-", "").lower()
        devices = [d for d in devices if d.serial.replace("-", "").lower() == normalized]
    else:
        usb_devices = [d for d in devices if d.connection_type.upper() == "USB"]
        if usb_devices:
            devices = usb_devices
    if not devices:
        raise UsbmuxError("No usbmux USB device found; wait for the ramdisk to finish booting")
    if len(devices) > 1 and not serial:
        raise UsbmuxError("More than one USB device is connected; pass --udid SERIAL")

    device = devices[0]
    sock = _open_mux(host, port)
    try:
        _send_plist(
            sock,
            {
                "MessageType": "Connect",
                "ClientVersionString": "ICH Windows 1.0",
                "ProgName": "ich-iproxy",
                "DeviceID": device.device_id,
                "PortNumber": socket.htons(remote_port),
            },
        )
        response = _recv_plist(sock)
        result = int(response.get("Number", -1))
        if result != 0:
            raise UsbmuxError(f"usbmux refused device port {remote_port} (error {result})")
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


def _pump(source: socket.socket, target: socket.socket, stopped: threading.Event) -> None:
    try:
        while not stopped.is_set():
            data = source.recv(64 * 1024)
            if not data:
                break
            target.sendall(data)
    except OSError:
        pass
    finally:
        stopped.set()
        try:
            target.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class ForwardServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, handler, *, remote_port: int, udid: str | None, mux_host: str, mux_port: int):
        self.remote_port = remote_port
        self.udid = udid
        self.mux_host = mux_host
        self.mux_port = mux_port
        super().__init__(server_address, handler)


class ForwardHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: ForwardServer = self.server  # type: ignore[assignment]
        try:
            remote = connect_device(
                server.remote_port,
                serial=server.udid,
                host=server.mux_host,
                port=server.mux_port,
            )
        except UsbmuxError as exc:
            print(f"iproxy: {exc}", file=sys.stderr)
            return
        stopped = threading.Event()
        with remote:
            upstream = threading.Thread(target=_pump, args=(self.request, remote, stopped), daemon=True)
            upstream.start()
            _pump(remote, self.request, stopped)
            upstream.join(timeout=1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward a TCP port through Windows usbmux")
    parser.add_argument("local_port", nargs="?", type=int, default=2222)
    parser.add_argument("remote_port", nargs="?", type=int, default=22)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--udid")
    parser.add_argument("--usbmux-host", default=USBMUX_HOST)
    parser.add_argument("--usbmux-port", type=int, default=USBMUX_PORT)
    parser.add_argument("--list", action="store_true", help="list usbmux devices and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.list:
            devices = list_devices(args.usbmux_host, args.usbmux_port)
            if not devices:
                print("No usbmux devices found.")
                return 2
            for device in devices:
                print(f"{device.device_id}\t{device.connection_type}\t{device.serial}")
            return 0

        with ForwardServer(
            (args.listen, args.local_port),
            ForwardHandler,
            remote_port=args.remote_port,
            udid=args.udid,
            mux_host=args.usbmux_host,
            mux_port=args.usbmux_port,
        ) as server:
            print(
                f"Forwarding {args.listen}:{args.local_port} -> device:{args.remote_port}. "
                "Press Ctrl+C to stop."
            )
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                print("\nStopped.")
        return 0
    except (UsbmuxError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
