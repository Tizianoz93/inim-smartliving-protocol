#!/usr/bin/env python3
"""
Bulk-read a SmartLiving panel's memory over TCP :5004 and save it to a file.

The read header's length byte is one byte, so the panel answers at most 256 bytes
per request; this walks the range in 256-byte chunks. Handy for offline analysis
(hunting name tables, diffing config, etc.) instead of polling the panel each time.

  export INIM_HOST=192.168.1.50
  python dump_memory.py --start 0 --end 0x20000 --out panel.bin

A companion <out>.strings.txt lists the printable 16-byte slots with their
addresses, which is usually where INIM keeps area/zone/output/user names.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

HANDSHAKE = b"pass"
HANDSHAKE_DELAY = 0.4
CHUNK = 256
NAME_LEN = 16


def decode_name_slot(raw: bytes) -> str:
    """Decode a 16-byte EEPROM label; see COMPATIBILITY.md for the byte-15 flag."""
    if len(raw) < NAME_LEN:
        raw = raw.ljust(NAME_LEN, b"\x00")
    else:
        raw = raw[:NAME_LEN]

    if raw[15] in (0x20, 0x00, 0xFF):
        text = raw
    elif raw[14] in (0x20, 0x00, 0xFF):
        text = raw[:15]
    else:
        text = raw

    cleaned = bytes(b for b in text if b not in (0x00, 0xFF))
    if len(cleaned) < 2 or not all(0x20 <= b <= 0x7E or 0xC0 <= b <= 0xFF for b in cleaned):
        return ""
    return cleaned.decode("cp1252", errors="replace").strip()


def build_read_frame(address: int, length: int, prefix: int = 0) -> bytes:
    body = bytearray(8)
    body[0] = prefix & 0xFF
    body[1] = (prefix >> 8) & 0xFF
    body[2] = (address >> 16) & 0xFF
    body[3] = (address >> 8) & 0xFF
    body[4] = address & 0xFF
    body[6] = (length - 1) & 0xFF
    body[7] = sum(body[:7]) & 0xFF
    return bytes(body)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        d = sock.recv(n - len(buf))
        if not d:
            break
        buf += d
    return buf


def dump(host: str, port: int, start: int, end: int, prefix: int) -> bytes:
    sock = socket.create_connection((host, port), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.sendall(HANDSHAKE)
    time.sleep(HANDSHAKE_DELAY)

    out = bytearray()
    addr = start
    total = end - start
    try:
        while addr < end:
            n = min(CHUNK, end - addr)
            chunk = b""
            for attempt in range(2):
                try:
                    sock.sendall(build_read_frame(addr, n, prefix))
                    chunk = recv_exact(sock, n)
                    if len(chunk) == n:
                        break
                except OSError:
                    chunk = b""
                time.sleep(0.2)
            if len(chunk) != n:
                # leave a gap marker so offsets stay aligned
                chunk = b"\xff" * n
            out += chunk
            addr += n
            done = addr - start
            sys.stderr.write(f"\r0x{addr:06X}  {done * 100 // total:3d}%")
            sys.stderr.flush()
    finally:
        sock.close()
    sys.stderr.write("\n")
    return bytes(out)


def write_strings(data: bytes, base: int, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for i in range(0, len(data) - NAME_LEN, NAME_LEN):
            slot = data[i:i + NAME_LEN]
            txt = decode_name_slot(slot)
            if len(txt) >= 2 and any(c.isalpha() for c in txt):
                fh.write(f"0x{base + i:06X}  {txt}\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump SmartLiving panel memory")
    ap.add_argument("--host", default=os.environ.get("INIM_HOST"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("INIM_PORT", "5004")))
    ap.add_argument("--start", type=lambda x: int(x, 0), default=0x0000)
    ap.add_argument("--end", type=lambda x: int(x, 0), default=0x20000)
    ap.add_argument("--prefix", type=lambda x: int(x, 0), default=0x0000,
                    help="read prefix (0x0000 standard, 0x0010 high-memory tables)")
    ap.add_argument("--out", default="panel.bin")
    args = ap.parse_args()
    if not args.host:
        ap.error("need --host or INIM_HOST")

    data = dump(args.host, args.port, args.start, args.end, args.prefix)
    with open(args.out, "wb") as fh:
        fh.write(data)
    strings_path = args.out + ".strings.txt"
    write_strings(data, args.start, strings_path)
    print(f"wrote {len(data)} bytes to {args.out}")
    print(f"wrote name candidates to {strings_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
