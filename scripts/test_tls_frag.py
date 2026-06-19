"""Probe whether TLS ClientHello fragmentation defeats the SNI block to api.telegram.org.

Runs a tiny local TCP relay that splits the first client->server write into segments
(so the SNI 'api.telegram.org' spans TCP boundaries), then attempts a real TLS handshake
to api.telegram.org *through* the relay. End-to-end TLS (cert validated as api.telegram.org).
Prints which split strategy (if any) lets the handshake complete.
"""
from __future__ import annotations

import socket
import ssl
import threading
import time

HOST = "api.telegram.org"
PORT = 443


def _resolve() -> str:
    return socket.gethostbyname(HOST)  # DNS works; only TLS/SNI is blocked


def _relay_once(listen_port: int, upstream_ip: str, split_offsets: list[int]) -> None:
    """Accept ONE connection, fragment the first client->server write at `split_offsets`."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen_port))
    srv.listen(1)
    srv.settimeout(20)
    try:
        cli, _ = srv.accept()
    except socket.timeout:
        srv.close()
        return
    up = socket.create_connection((upstream_ip, PORT), timeout=10)
    up.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    first = {"done": False}
    # split_offsets semantics: [-1] = locate the SNI host bytes and split mid-string;
    # [-2] = byte-by-byte for the first 200 bytes; otherwise explicit byte offsets.
    def _segments(data: bytes) -> list[bytes]:
        if split_offsets == [-1]:
            idx = data.find(HOST.encode())
            if idx < 0:
                return [data]
            mid = idx + len(HOST) // 2
            return [data[:mid], data[mid:]]
        if split_offsets == [-2]:
            n = min(200, len(data))
            return [data[i:i + 1] for i in range(n)] + ([data[n:]] if len(data) > n else [])
        segs, prev = [], 0
        for off in split_offsets:
            segs.append(data[prev:off]); prev = off
        segs.append(data[prev:])
        return segs

    def c2s():
        while True:
            try:
                data = cli.recv(65536)
            except OSError:
                break
            if not data:
                break
            if not first["done"] and len(data) > 20:
                first["done"] = True
                for seg in _segments(data):
                    if not seg:
                        continue
                    up.sendall(seg)
                    time.sleep(0.01)  # force separate TCP segments
            else:
                try:
                    up.sendall(data)
                except OSError:
                    break
        try:
            up.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    def s2c():
        while True:
            try:
                data = up.recv(65536)
            except OSError:
                break
            if not data:
                break
            try:
                cli.sendall(data)
            except OSError:
                break
        try:
            cli.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    t1 = threading.Thread(target=c2s, daemon=True)
    t2 = threading.Thread(target=s2c, daemon=True)
    t1.start(); t2.start()
    t1.join(timeout=25); t2.join(timeout=25)
    for s in (cli, up, srv):
        try:
            s.close()
        except OSError:
            pass


def _try(split_offsets: list[int] | None, upstream_ip: str) -> str:
    """Attempt a TLS handshake. If split_offsets is None, go direct (baseline)."""
    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        if split_offsets is None:
            raw = socket.create_connection((HOST, PORT), timeout=12)
        else:
            port = 0
            # bind an ephemeral relay port first
            probe = socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()
            threading.Thread(target=_relay_once, args=(port, upstream_ip, split_offsets), daemon=True).start()
            time.sleep(0.3)
            raw = socket.create_connection(("127.0.0.1", port), timeout=12)
        ssock = ctx.wrap_socket(raw, server_hostname=HOST)
        proto = ssock.version()
        ssock.close()
        return f"OK  TLS {proto}  {int((time.time()-t0)*1000)}ms"
    except Exception as e:
        return f"FAIL {int((time.time()-t0)*1000)}ms  {type(e).__name__}: {str(e)[:50]}"


def main() -> None:
    ip = _resolve()
    print(f"api.telegram.org -> {ip}")
    print(f"{'baseline (direct, no frag)':38} {_try(None, ip)}")
    for desc, offs in [
        ("split mid-SNI (find host)", [-1]),
        ("byte-by-byte first 200B", [-2]),
        ("split @80", [80]),
        ("split @120", [120]),
        ("split @64,@128", [64, 128]),
    ]:
        print(f"{('frag ' + desc):38} {_try(offs, ip)}")


if __name__ == "__main__":
    main()
