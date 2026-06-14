#!/usr/bin/env python3
"""Prove @0x2001..0x2003 are separate registers, not offsets in a @0x2000 block."""

from __future__ import annotations

import os
import socket
import sys
import time

HOST = os.environ.get("INIM_HOST", "192.168.1.121")
PORT = int(os.environ.get("INIM_PORT", "5004"))
M = 20
NUM_AREAS = 5
ZONE_POLLS = [1, 3, 5, 6, 7, 17]
OUTPUTS = [4, 20, 21, 22]


def build_read_frame(address: int, response_len: int) -> bytes:
    body = bytearray(8)
    body[2] = (address >> 16) & 0xFF
    body[3] = (address >> 8) & 0xFF
    body[4] = address & 0xFF
    body[6] = (response_len - 1) & 0xFF
    body[7] = sum(body[:7]) & 0xFF
    return bytes(body)


def strip_read_checksum(raw: bytes) -> bytes:
    if len(raw) >= 2 and (sum(raw[:-1]) & 0xFF) == raw[-1]:
        return raw[:-1]
    return raw


def area_len(num_areas: int) -> int:
    return max(14, num_areas // 2 + 1 + 10)


def tr_min(max_poll: int) -> int:
    return (max_poll - 1) // 4 + 1


def z1_min(max_poll: int) -> int:
    return (max_poll - 1) // 8 + 1


def z2_min(num_terminals: int, outputs: list[int]) -> int:
    if not outputs:
        return 0
    return (num_terminals * 2 // 8 + 1) + (num_terminals // 8 + 1) + max(outputs) // 8 + 1


def read_at(sock: socket.socket, address: int, length: int) -> bytes:
    out = bytearray()
    remaining = length
    addr = address
    while remaining > 0:
        wire = min(256, remaining + 1)
        sock.sendall(build_read_frame(addr, wire))
        raw = sock.recv(wire)
        data = strip_read_checksum(raw)
        out += data
        addr += len(data)
        remaining -= len(data)
    return bytes(out)


def main() -> int:
    max_poll = max(ZONE_POLLS)
    al = area_len(NUM_AREAS)
    tr = tr_min(max_poll)
    z1 = z1_min(max_poll)
    z2 = z2_min(M, OUTPUTS)
    total = al + tr + z1 + z2

    sock = socket.create_connection((HOST, PORT), timeout=8)
    sock.settimeout(8)
    sock.sendall(b"pass")
    time.sleep(0.4)

    block = read_at(sock, 0x2000, total)
    sep_a = read_at(sock, 0x2000, al)
    sep_tr = read_at(sock, 0x2001, tr)
    sep_z1 = read_at(sock, 0x2002, z1)
    sep_z2 = read_at(sock, 0x2003, z2)
    sock.close()

    areas_ok = block[:al] == sep_a
    tr_ok = block[al : al + tr] == sep_tr
    z1_ok = block[al + tr : al + tr + z1] == sep_z1
    z2_ok = block[al + tr + z1 :] == sep_z2

    print(f"Read plan: areas={al} tr={tr} z1={z1} z2={z2} (total {total} B from @0x2000)")
    print(f"  areas slice == @0x2000 read: {'OK' if areas_ok else 'MISMATCH'}")
    print(f"  tr slice    == @0x2001 read: {'OK' if tr_ok else 'MISMATCH'}")
    print(f"  z1 slice    == @0x2002 read: {'OK' if z1_ok else 'MISMATCH'}")
    print(f"  z2 slice    == @0x2003 read: {'OK' if z2_ok else 'MISMATCH'}")

    if areas_ok and not (tr_ok and z1_ok and z2_ok):
        print(
            "\nConclusion: @0x2001..0x2003 are separate logical registers. "
            "Use four frame reads, not one long read from @0x2000."
        )
        return 0

    if areas_ok and tr_ok and z1_ok and z2_ok:
        print("\nAll slices match (unexpected on 515 — panel may treat RAM as flat).")
        return 0

    print("\nUnexpected mismatch pattern.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
