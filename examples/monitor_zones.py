#!/usr/bin/env python3
"""List configured zones/outputs and poll zone state for N seconds.

515 / fw 6.x profile. Close SmartLeague first (one TCP session).

  set INIM_HOST=192.168.1.121
  python monitor_zones.py
  python monitor_zones.py --seconds 10
"""

from __future__ import annotations

import argparse
import os
import re
import socket
import sys
import time
from dataclasses import dataclass

DEFAULT_HOST = os.environ.get("INIM_HOST", "192.168.1.121")
DEFAULT_PORT = int(os.environ.get("INIM_PORT", "5004"))
HANDSHAKE_DELAY = 0.4
IO_TIMEOUT = 8.0

ADDR_ZONE_TR = 0x002001
ADDR_ZONE1 = 0x002002
ADDR_ZONE2 = 0x002003

def strip_read_checksum(raw: bytes) -> bytes:
    if len(raw) >= 2 and (sum(raw[:-1]) & 0xFF) == raw[-1]:
        return raw[:-1]
    return raw


def decode_firmware_version(raw: bytes) -> str:
    return strip_read_checksum(raw).decode("ascii", errors="replace").strip()


M = 20  # 515 MAX_NUM_TERMINALI_LOGICI
FIRMWARE_DATA_LEN = 12
NAME_LEN = 16
TERMINAL_TIPO_ZONA = 0
TERMINAL_TIPO_USCITA = 1
TERMINAL_TIPO_DOUBLE = 3

PROFILE = {
    "terminal_names": 0x172F0,
    "terminal_phys_names": 0x17430,
    "terminal_config": 0x14368,
    "terminal_active_map": 0x14595,
    "terminal_active_map_len": 66,
    "onboard_output_summary": 0x1315F,
    "output_names_onboard": 0x17FA0,
    "onboard_fixed_outputs": 3,
}

TERMINAL_STATE = {
    0: "riposo",
    1: "allarme/aperto",
    2: "cortocircuito",
    3: "guasto/sbil.",
}

_LOC_EXPANSION = re.compile(r"espans\.?\s+0*(\d+)\s+t0*(\d+)", re.I)
_LOC_CENTRAL = re.compile(r"centrale\s+t0*(\d+)", re.I)
_LOC_KEYPAD = re.compile(r"tast\.?\s+0*(\d+)\s+t0*(\d+)", re.I)


def build_read_frame(address: int, response_len: int) -> bytes:
    body = bytearray(8)
    body[2] = (address >> 16) & 0xFF
    body[3] = (address >> 8) & 0xFF
    body[4] = address & 0xFF
    body[6] = (response_len - 1) & 0xFF
    body[7] = sum(body[:7]) & 0xFF
    return bytes(body)


def decode_name(raw: bytes) -> str:
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
    meaningful = [b for b in text if b not in (0x00, 0xFF)]
    if not meaningful:
        return ""
    printable = [b for b in meaningful if 0x20 <= b <= 0x7E or 0xC0 <= b <= 0xFF]
    if len(printable) / len(meaningful) < 0.7:
        return ""
    return bytes(printable).decode("cp1252", errors="replace").strip()


def tr_zone_buffer_len(num_terminals: int) -> int:
    return (num_terminals * 2 * 2 + 7) // 8 + 1


def zone1_buffer_len(num_terminals: int) -> int:
    section = (num_terminals * 2 + 7) // 8 + 1
    return section + section


def zone_poll_number(terminal_index: int, half: int = 0) -> int:
    return terminal_index * 2 + half + 1


def zone_terminal_state(tr_zone: bytes, zone: int) -> int | None:
    z = zone - 1
    idx = z // 4
    if idx >= len(tr_zone):
        return None
    return (tr_zone[idx] >> ((z % 4) * 2)) & 0x03


def zone_excluded(zone1: bytes, zone: int) -> bool:
    z = zone - 1
    if z // 8 >= len(zone1):
        return False
    return bool((zone1[z // 8] >> (z % 8)) & 0x01)


def output_status_offset(num_terminals: int) -> int:
    return (num_terminals * 2 // 8 + 1) + (num_terminals // 8 + 1)


def output_on(zone2: bytes, index: int, num_terminals: int) -> bool:
    byte_idx = output_status_offset(num_terminals) + index // 8
    if byte_idx >= len(zone2):
        return False
    return bool((zone2[byte_idx] >> (index % 8)) & 0x01)


def parse_active_indexes(raw: bytes) -> list[int]:
    return sorted(b for b in raw if b != 0xFF and b < 0x80)


def parse_terminal_types(raw: bytes, count: int) -> list[int]:
    types: list[int] = []
    for i in range(count):
        off = i * 12 + 11
        types.append(raw[off] if off < len(raw) else TERMINAL_TIPO_ZONA)
    return types


def is_double_partner(label: str) -> bool:
    text = (label or "").strip()
    if not text:
        return False
    if _LOC_CENTRAL.search(text) or _LOC_EXPANSION.search(text) or _LOC_KEYPAD.search(text):
        return False
    return True


@dataclass
class ZoneEntry:
    poll: int
    name: str
    terminal_index: int
    half: int


@dataclass
class OutputEntry:
    index: int
    name: str


class Panel:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=IO_TIMEOUT)
        self.sock.settimeout(IO_TIMEOUT)
        self.sock.sendall(b"pass")
        time.sleep(HANDSHAKE_DELAY)

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def read(self, address: int, length: int) -> bytes:
        assert self.sock is not None
        out = bytearray()
        remaining = length
        addr = address
        while remaining > 0:
            wire = min(256, remaining + 1)
            self.sock.sendall(build_read_frame(addr, wire))
            raw = self.sock.recv(wire)
            data = strip_read_checksum(raw)
            out += data
            addr += len(data)
            remaining -= len(data)
        return bytes(out)

    def read_names(self, base: int, count: int) -> list[str]:
        raw = self.read(base, count * NAME_LEN)
        return [decode_name(raw[i * NAME_LEN : (i + 1) * NAME_LEN]) for i in range(count)]

    def discover_zones(self) -> list[ZoneEntry]:
        active = parse_active_indexes(
            self.read(PROFILE["terminal_active_map"], PROFILE["terminal_active_map_len"])
        )
        types = parse_terminal_types(self.read(PROFILE["terminal_config"], M * 12), M)
        names_a = self.read_names(PROFILE["terminal_names"], M)
        names_b = self.read_names(PROFILE["terminal_phys_names"], M)
        zones: list[ZoneEntry] = []
        for i in active:
            if i >= len(types) or types[i] not in (TERMINAL_TIPO_ZONA, TERMINAL_TIPO_DOUBLE):
                continue
            primary = names_a[i].strip()
            if not primary:
                continue
            zones.append(
                ZoneEntry(zone_poll_number(i, 0), primary, i, 0)
            )
            partner = names_b[i].strip() if i < len(names_b) else ""
            if is_double_partner(partner) or types[i] == TERMINAL_TIPO_DOUBLE:
                label = partner or f"{primary} (2)"
                zones.append(
                    ZoneEntry(zone_poll_number(i, 1), label, i, 1)
                )
        return zones

    def discover_outputs(self) -> list[OutputEntry]:
        active = parse_active_indexes(
            self.read(PROFILE["terminal_active_map"], PROFILE["terminal_active_map_len"])
        )
        types = parse_terminal_types(self.read(PROFILE["terminal_config"], M * 12), M)
        names_a = self.read_names(PROFILE["terminal_names"], M)
        summary = self.read(PROFILE["onboard_output_summary"], 7)
        onboard_n = summary[1] if len(summary) >= 2 and summary[1] else PROFILE["onboard_fixed_outputs"]
        onboard_names = self.read_names(PROFILE["output_names_onboard"], onboard_n)
        outputs: list[OutputEntry] = []
        for i in active:
            if i < len(types) and types[i] == TERMINAL_TIPO_USCITA:
                name = names_a[i].strip()
                if name:
                    outputs.append(OutputEntry(i, name))
        for j, raw in enumerate(onboard_names):
            if j >= onboard_n:
                break
            name = raw.strip()
            if name:
                outputs.append(OutputEntry(M + j, name))
        return outputs

    def poll_zones(self, polls: list[int]) -> dict[int, tuple[int, bool]]:
        tr = self.read(ADDR_ZONE_TR, tr_zone_buffer_len(M))
        z1 = self.read(ADDR_ZONE1, zone1_buffer_len(M))
        out: dict[int, tuple[int, bool]] = {}
        for p in polls:
            st = zone_terminal_state(tr, p)
            if st is not None:
                out[p] = (st, zone_excluded(z1, p))
        return out

    def poll_outputs(self, indexes: list[int]) -> dict[int, bool]:
        if not indexes:
            return {}
        z2_len = output_status_offset(M) + max(indexes) // 8 + 1
        z2 = self.read(ADDR_ZONE2, z2_len)
        return {i: output_on(z2, i, M) for i in indexes}


def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor INIM zones/outputs")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--interval", type=float, default=0.5)
    args = ap.parse_args()

    panel = Panel(args.host, args.port)
    try:
        print(f"Connessione a {args.host}:{args.port} …")
        panel.connect()
        version = decode_firmware_version(panel.read(0x004000, FIRMWARE_DATA_LEN))
        print(f"Firmware: {version}\n")

        zones = panel.discover_zones()
        outputs = panel.discover_outputs()

        print("=== ZONE CONFIGURATE ===")
        for z in zones:
            half = " [doppia]" if z.half else ""
            print(f"  poll {z.poll:2d}  term idx {z.terminal_index:2d}{half}  {z.name}")
        print(f"\n=== USCITE ({len(outputs)}) ===")
        for o in outputs:
            print(f"  idx {o.index:2d}  {o.name}")

        polls = [z.poll for z in zones]
        out_idx = [o.index for o in outputs]
        highlight = {z.poll for z in zones if "PIR_BALCONE" in z.name.upper() or "AM_BALCONE" in z.name.upper()}

        print(f"\n=== MONITORAGGIO {args.seconds:.0f}s (trigger PIR_BALCONE ora) ===")
        print("  (0=riposo  1=allarme/aperto  2=CC  3=guasto)\n")
        deadline = time.monotonic() + args.seconds
        prev: dict[int, tuple[int, bool]] = {}
        prev_out: dict[int, bool] = {}
        tick = 0
        while time.monotonic() < deadline:
            states = panel.poll_zones(polls)
            ostates = panel.poll_outputs(out_idx)
            changed = states != prev or ostates != prev_out
            if changed or tick == 0:
                ts = time.strftime("%H:%M:%S")
                parts = []
                for z in zones:
                    st, excl = states.get(z.poll, (None, False))
                    if st is None:
                        continue
                    lab = TERMINAL_STATE.get(st, str(st))
                    mark = " ***" if z.poll in highlight and st != 0 else ""
                    ex = " [escl.]" if excl else ""
                    parts.append(f"{z.name}({z.poll})={lab}{ex}{mark}")
                out_parts = [
                    f"{o.name}({'ON' if ostates.get(o.index) else 'off'})"
                    for o in outputs
                ]
                line = f"[{ts}] " + " | ".join(parts)
                if out_parts:
                    line += " || " + ", ".join(out_parts)
                print(line)
                prev = states
                prev_out = ostates
            tick += 1
            time.sleep(args.interval)

        print("\nFine monitoraggio.")
    except (OSError, TimeoutError, ConnectionError) as err:
        print(f"Errore: {err}", file=sys.stderr)
        print("Chiudi SmartLeague e verifica INIM_HOST.", file=sys.stderr)
        return 1
    finally:
        panel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
