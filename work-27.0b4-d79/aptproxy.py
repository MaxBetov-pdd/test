#!/usr/bin/env python3
"""Minimal HTTP/CONNECT proxy so the device's apt can reach the internet.

The device has working WiFi but no usable DNS for command-line tools: the
system resolver reads /private/etc/resolv.conf, which does not exist and sits
on the read-only System volume. The bootstrap ships its own resolv.conf at
/var/jb/etc/resolv.conf, but nothing consults that path.

Rather than an SSHRD trip to create the file, apt is pointed at this proxy on
the Mac, which does the name resolution. /var/jb/etc/apt/apt.conf.d/ is on the
Data volume and writable on a normal boot.

Binds to this machine's LAN address by default, detected at startup, never to
0.0.0.0, so it is not exposed more widely than needed. Override with --bind.

If you are reaching the Mac through an `ssh -R 8899:127.0.0.1:8899` tunnel
rather than over WiFi, bind to loopback instead: the tunnel delivers to
127.0.0.1, so a proxy bound only to the LAN address will not see the traffic.

Stop it with Ctrl-C or by killing the process; nothing persists.
"""

import argparse
import select
import socket
import socketserver
import threading

BUFSZ = 65536
TIMEOUT = 30


class ProxyHandler(socketserver.BaseRequestHandler):
    """Handles both CONNECT (https) and absolute-URI GET (plain http)."""

    def handle(self):
        self.request.settimeout(TIMEOUT)
        try:
            head = self._read_headers()
        except (OSError, ValueError):
            return
        if not head:
            return

        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) < 3:
            return
        method, target = parts[0], parts[1]

        if method.upper() == "CONNECT":
            self._do_connect(target)
        else:
            self._do_plain(target, head)

    def _read_headers(self):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(BUFSZ)
            if not chunk:
                break
            data += chunk
            if len(data) > 1 << 20:
                raise ValueError("header too large")
        return data

    def _do_connect(self, target):
        host, _, port = target.rpartition(":")
        try:
            upstream = socket.create_connection((host, int(port)), timeout=TIMEOUT)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        self._pump(self.request, upstream)

    def _do_plain(self, target, head):
        # target is an absolute URI: http://host[:port]/path
        rest = target.split("://", 1)[-1]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        try:
            upstream = socket.create_connection((host, int(port or 80)), timeout=TIMEOUT)
        except OSError:
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        # rewrite the absolute URI to an origin-form path for the real server
        first, _, tail = head.partition(b"\r\n")
        method = first.split()[0]
        ver = first.split()[-1]
        upstream.sendall(b"%s /%s %s\r\n%s" % (method, path.encode(), ver, tail))
        self._pump(self.request, upstream)

    @staticmethod
    def _pump(a, b):
        socks = [a, b]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, TIMEOUT)
                if x or not r:
                    break
                for s in r:
                    other = b if s is a else a
                    data = s.recv(BUFSZ)
                    if not data:
                        return
                    other.sendall(data)
        except OSError:
            pass
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass


class Threaded(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def default_bind():
    """This machine's primary LAN address.

    The UDP socket is only used to ask the routing table which local address would
    be used to reach the outside world; connect() on UDP sends nothing. Falls back
    to loopback, which is correct when the device reaches the Mac over an ssh -R
    tunnel rather than over WiFi.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))       # TEST-NET-1, never routed anywhere
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default=None,
                    help="address to listen on (default: this machine's LAN address)")
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    if args.bind is None:
        args.bind = default_bind()

    srv = Threaded((args.bind, args.port), ProxyHandler)
    print(f"[+] proxy listening on {args.bind}:{args.port}")
    print(f"[.] point apt at:  Acquire::http::Proxy \"http://{args.bind}:{args.port}\";")
    srv.serve_forever()


if __name__ == "__main__":
    main()
