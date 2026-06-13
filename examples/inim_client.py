#!/usr/bin/env python3
"""
Talk to an INIM SmartLiving panel on TCP port 5004 (SmartLAN/SI module).

Figured out by sniffing SmartLeague traffic and reading Centrale5.dll.
Close SmartLeague first — the panel only accepts one TCP session.

  export INIM_HOST=192.168.1.50
  export INIM_PIN=1234
  python inim_client.py --host $INIM_HOST status
  python inim_client.py --host $INIM_HOST status --json
  python inim_client.py arm --mode away --area 1 --code $INIM_PIN --yes
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_PORT = int(os.environ.get("INIM_PORT", "5004"))
DEFAULT_AREAS = 5
HANDSHAKE_DELAY = 0.4  # without this, "pass" and the first frame get coalesced
CONNECT_TIMEOUT = 10.0
IO_TIMEOUT = 8.0
MAX_READ_CHUNK = 256  # length field in the read header is one byte

# RAM map from Centrale5.dll / InizializzaRam_5_00 (same on every model we checked)
ADDR_VERSION = 0x004000
ADDR_STATUS = 0x002000       # com_read_stato_area
ADDR_STATUS_ZONE_TR = 0x002001
ADDR_STATUS_ZONE1 = 0x002002
ADDR_STATUS_ZONE2 = 0x002003
ADDR_ESITO = 0x002004        # command result poll
ADDR_CMD_AREA = 0x002006     # EseguiComando writes here
ADDR_CMD_USCITE = 0x002007
ADDR_CMD_RESET_AREA = 0x002008
ADDR_CMD_ZONE = 0x002009

# 16-byte ASCII labels; fw <= 5.x uses pointers, fw 6.x needs direct addresses (see below)
NAME_LEN = 16
PTR_NAMES_AREAS = 0x4014
PTR_NAMES_ZONES = 0x4016
PTR_NAMES_SCENARIOS = 0x4024
# found on a 515 running fw 6.09 — pointer at 0x4014 points at garbage there
DIRECT_AREA_NAMES = (0x172A0,)
DIRECT_ZONE_NAMES = (0x172F0,)
DIRECT_SCENARIO_NAMES = (0x17CF0,)

PREFIX_READ = 0x0000
PREFIX_WRITE = 0x0001

# mode nibble values — match EseguiComando argument and status readback
MODE_TOTAL = 1
MODE_PARTIAL = 2
MODE_INSTANT = 3
MODE_DISARM = 4
MODE_NOT_PRESENT = 0

ARM_MODES = {
    "total": MODE_TOTAL,
    "away": MODE_TOTAL,
    "partial": MODE_PARTIAL,
    "stay": MODE_PARTIAL,
    "instant": MODE_INSTANT,
}
ARMED_MODES = {MODE_TOTAL, MODE_PARTIAL, MODE_INSTANT}

MODE_LABELS = {
    MODE_TOTAL: "armed away",
    MODE_PARTIAL: "armed stay",
    MODE_INSTANT: "armed instant",
    MODE_DISARM: "disarmed",
    MODE_NOT_PRESENT: "not configured",
}

PIN_LEN = 6
AREA_DATA_LEN = 8

# ModoIns EEPROM — 0x142BF on 515 fw 6.x; older tables below for other boards
SCENARIO_CFG_ADDRS = (0x142BF, 0x2ED5, 0x3572, 0x5F9C, 0xA9F2)
SCENARIO_COUNT_MAX = 30

TERMINAL_STATE = {
    0: "rest",
    1: "alarm",
    2: "short circuit",
    3: "line fault",
}


def area_state_label(value: int) -> str:
    return MODE_LABELS.get(value, f"unknown (0x{value:X})")


def area_nibble(data: bytes, area: int) -> int | None:
    """Two areas per byte at 0x2000 — see AreeRealTime in Centrale5.dll."""
    p = area - 1
    idx = p // 2
    if idx >= len(data):
        return None
    return (data[idx] & 0x0F) if p % 2 == 0 else (data[idx] >> 4) & 0x0F


def is_armed(value: int) -> bool:
    return value in ARMED_MODES


def is_disarmed(value: int) -> bool:
    return value == MODE_DISARM


@dataclass
class PanelStatus:
    raw: bytes
    areas: list[tuple[int, int | None]]

    @property
    def summary(self) -> str:
        armed = [n for n, v in self.areas if v is not None and is_armed(v)]
        if armed:
            return f"armed: area {', '.join(str(n) for n in armed)}"
        return "all disarmed"


def decode_name(raw: bytes) -> str:
    """One 16-byte name slot. Panels pad with spaces; junk slots fail the ASCII check."""
    cleaned = bytes(b for b in raw if b not in (0x00, 0xFF))
    if not cleaned or not all(0x20 <= b <= 0x7E for b in cleaned):
        return ""
    return " ".join(cleaned.decode("ascii").split())


def clean_label(name: str) -> str:
    return " ".join(name.split())


def name_looks_valid(name: str) -> bool:
    if len(name) < 2:
        return False
    ok = sum(1 for c in name if c.isalnum() or c in " .'_/-")
    return ok / len(name) >= 0.75


def names_look_valid(names: list[str]) -> bool:
    valid = [n for n in names if name_looks_valid(n)]
    return len(valid) >= max(1, min(2, len(names) // 3))


def pointer_base_plausible(base: int) -> bool:
    # fw 6.x often returns 0xA0A0 here — real string blocks live around 0x17xxx
    return 0x8000 <= base <= 0x40000


def build_read_frame(address: int, response_len: int, prefix: int = PREFIX_READ) -> bytes:
    body = bytearray(8)
    body[0] = prefix & 0xFF
    body[1] = (prefix >> 8) & 0xFF
    body[2] = (address >> 16) & 0xFF
    body[3] = (address >> 8) & 0xFF
    body[4] = address & 0xFF
    body[6] = (response_len - 1) & 0xFF
    body[7] = sum(body[:7]) & 0xFF
    return bytes(body)


def build_write_frame(address: int, payload: bytes) -> bytes:
    head = bytearray(8)
    head[0] = PREFIX_WRITE & 0xFF
    head[1] = (PREFIX_WRITE >> 8) & 0xFF
    head[2] = (address >> 16) & 0xFF
    head[3] = (address >> 8) & 0xFF
    head[4] = address & 0xFF
    head[6] = len(payload) & 0xFF
    head[7] = sum(head[:7]) & 0xFF
    return bytes(head) + payload


def encode_pin(pin: str) -> bytes:
    """PadRight(6) from EseguiComando — missing digits become 0xFF."""
    digits = pin.strip()
    if not digits.isdigit() or not (4 <= len(digits) <= PIN_LEN):
        raise ValueError(f"invalid PIN {pin!r} (want 4–{PIN_LEN} digits)")
    out = bytearray(PIN_LEN)
    for i in range(PIN_LEN):
        out[i] = int(digits[i]) if i < len(digits) else 0xFF
    return bytes(out)


def encode_area_action(area: int, mode: int, num_areas: int = DEFAULT_AREAS) -> bytes:
    if not 1 <= area <= num_areas:
        raise ValueError(f"bad area {area} (panel has {num_areas})")
    buf = bytearray(AREA_DATA_LEN)
    p = area - 1
    idx = p // 2
    if p % 2 == 0:
        buf[idx] = mode & 0x0F
    else:
        buf[idx] = (mode << 4) & 0xF0
    return bytes(buf)


def build_panel_command(pin: str, area: int, mode: int) -> bytes:
    payload = encode_pin(pin) + encode_area_action(area, mode)
    return build_write_frame(ADDR_CMD_AREA, payload)


def hexdump(data: bytes, prefix: str = "  ") -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{i:04X}  {hex_part:<47}  {asc}")
    return "\n".join(lines)


def parse_status(data: bytes, num_areas: int = DEFAULT_AREAS) -> PanelStatus:
    areas = [(a, area_nibble(data, a)) for a in range(1, num_areas + 1)]
    return PanelStatus(raw=data, areas=areas)


def format_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    if not rows:
        return ""
    widths = [max(len(headers[i]), *(len(r[i]) for r in rows)) for i in range(len(headers))]
    sep = "  ".join("-" * w for w in widths)
    head = "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers)))
    body = ["  ".join(row[i].ljust(widths[i]) for i in range(len(headers))) for row in rows]
    return "\n".join([head, sep, *body])


def area_label_from_names(names: list[str] | None, area: int) -> str | None:
    if not names or area - 1 >= len(names) or not names[area - 1]:
        return None
    label = clean_label(names[area - 1])
    return label if name_looks_valid(label) else None


def status_rows(status: PanelStatus, names: list[str] | None) -> list[tuple[str, ...]]:
    rows = []
    for num, value in status.areas:
        if value is None:
            rows.append((str(num), "-", "no data"))
            continue
        label = area_label_from_names(names, num)
        rows.append((str(num), label or "-", area_state_label(value)))
    return rows


def format_status(status: PanelStatus, names: list[str] | None = None) -> str:
    table = format_table(("Area", "Name", "Status"), status_rows(status, names))
    return f"{status.summary}\n\n{table}"


def status_as_dict(status: PanelStatus, names: list[str] | None = None) -> dict[str, Any]:
    areas_out = []
    for num, value in status.areas:
        entry: dict[str, Any] = {"area": num}
        label = area_label_from_names(names, num)
        if label:
            entry["name"] = label
        if value is None:
            entry["status"] = "no data"
        else:
            entry["mode"] = value
            entry["status"] = area_state_label(value)
            entry["armed"] = is_armed(value)
            entry["disarmed"] = is_disarmed(value)
        areas_out.append(entry)
    return {
        "summary": status.summary,
        "raw_hex": status.raw.hex(),
        "areas": areas_out,
    }


def zone_terminal_state(tr_zone: bytes, zone: int) -> int | None:
    z = zone - 1
    idx = z // 4
    if idx >= len(tr_zone):
        return None
    return (tr_zone[idx] >> ((z % 4) * 2)) & 0x03


def zone_bit(buf: bytes, zone: int) -> int | None:
    z = zone - 1
    idx = z // 8
    if idx >= len(buf):
        return None
    return (buf[idx] >> (z % 8)) & 0x01


@dataclass
class ZoneStatus:
    tr_zone: bytes
    zone1: bytes
    zone2: bytes
    zones: list[tuple[int, int, bool]]


def parse_zone_status(tr_zone: bytes, zone1: bytes, num_zones: int) -> ZoneStatus:
    zones = []
    for z in range(1, num_zones + 1):
        st = zone_terminal_state(tr_zone, z)
        if st is None:
            continue
        zones.append((z, st, zone_bit(zone1, z) == 1))
    return ZoneStatus(tr_zone=tr_zone, zone1=zone1, zone2=b"", zones=zones)


def format_zone_status(zs: ZoneStatus, zone_names: list[str] | None = None) -> str:
    lines = [f"Zones ({len(zs.zones)}):"]
    for num, st, excl in zs.zones:
        lab = TERMINAL_STATE.get(st, str(st))
        suffix = " [bypassed]" if excl else ""
        tag = ""
        if zone_names and num - 1 < len(zone_names) and zone_names[num - 1]:
            tag = f' "{clean_label(zone_names[num - 1])}"'
        lines.append(f"  Zone {num}{tag}: {lab}{suffix}")
    return "\n".join(lines)


def scenario_area_mode(modo: bytes, area: int) -> int:
    p = area - 1
    idx = p // 2
    if idx >= len(modo):
        return 0
    return (modo[idx] & 0x0F) if p % 2 == 0 else (modo[idx] >> 4) & 0x0F


def parse_scenarios(
    cfg: bytes,
    stride: int,
    num_areas: int = DEFAULT_AREAS,
    count: int = SCENARIO_COUNT_MAX,
) -> list[dict]:
    out = []
    modo_len = num_areas // 2 + 1
    for i in range(count):
        chunk = cfg[i * stride : i * stride + stride]
        if len(chunk) < modo_len:
            break
        modo = chunk[:modo_len]
        modes = {a: scenario_area_mode(modo, a) for a in range(1, num_areas + 1)}
        if all(m == 0 for m in modes.values()):
            continue
        out.append({"index": i, "modo": modo, "area_modes": modes})
    return out


def scenario_is_active(area_modes: dict[int, int], status: PanelStatus) -> bool:
    current = {a: v for a, v in status.areas}
    targets = {a: m for a, m in area_modes.items() if m != 0}
    if not targets:
        return False
    return all(current.get(a) == m for a, m in targets.items())


def ack_label(byte_val: int) -> str:
    known = {
        0x05: "PIN step ok",
        0x06: "partial PIN step ok",
        0x07: "instant PIN step ok",
        0x08: "accepted",
        0x0F: "away arm ok",
        0x14: "still armed",
        0x24: "stay ack",
        0x34: "instant ack",
        0x44: "disarmed",
    }
    ch = chr(byte_val) if 32 <= byte_val < 127 else None
    extra = f" ({ch!r})" if ch else ""
    return f"0x{byte_val:02X}{extra} {known.get(byte_val, '?')}"


class InimClient:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        verbose: bool = False,
        num_areas: int = DEFAULT_AREAS,
        area_names_addr: int | None = None,
        zone_names_addr: int | None = None,
        scenario_names_addr: int | None = None,
    ):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.num_areas = num_areas
        self._area_names_addr = area_names_addr
        self._zone_names_addr = zone_names_addr
        self._scenario_names_addr = scenario_names_addr
        self._area_names_raw: list[str] | None = None
        self._area_names_display: list[str] | None = None
        self._zone_names_raw: dict[int, list[str]] = {}
        self._scenario_names_raw: list[str] | None = None
        self._sock: Optional[socket.socket] = None

    def __enter__(self) -> InimClient:
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, file=sys.stderr)

    def connect(self) -> None:
        self._sock = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock.settimeout(IO_TIMEOUT)
        self._log(f"connected {self.host}:{self.port}")
        self._send(b"pass")
        time.sleep(HANDSHAKE_DELAY)

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        self._log(f"TX {len(data)}\n{hexdump(data)}")
        self._sock.sendall(data)

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("panel closed the connection")
            buf.extend(chunk)
        data = bytes(buf)
        self._log(f"RX {len(data)}\n{hexdump(data)}")
        return data

    def read_memory(self, address: int, length: int, prefix: int = PREFIX_READ) -> bytes:
        out = bytearray()
        addr = address
        left = length
        while left > 0:
            chunk = min(MAX_READ_CHUNK, left)
            self._send(build_read_frame(addr, chunk, prefix))
            out += self._recv_exact(chunk)
            addr += chunk
            left -= chunk
        return bytes(out)

    def read_names_at(self, base: int, count: int) -> list[str]:
        raw = self.read_memory(base, count * NAME_LEN)
        return [decode_name(raw[i * NAME_LEN : (i + 1) * NAME_LEN]) for i in range(count)]

    def _resolve_names(
        self,
        count: int,
        explicit: int | None,
        pointer: int,
        direct: tuple[int, ...],
    ) -> list[str]:
        if explicit is not None:
            return self.read_names_at(explicit, count)

        for addr in direct:
            try:
                names = self.read_names_at(addr, count)
                if names_look_valid(names):
                    self._log(f"names @ 0x{addr:05X}")
                    return names
            except (ConnectionError, TimeoutError, OSError):
                continue

        try:
            ptr = self.read_memory(pointer, 2)
            base = ptr[0] | (ptr[1] << 8)
            if pointer_base_plausible(base):
                names = self.read_names_at(base, count)
                if names_look_valid(names):
                    self._log(f"names via ptr 0x{pointer:04X} -> 0x{base:04X}")
                    return names
        except (ConnectionError, TimeoutError, OSError, ValueError):
            pass

        return [""] * count

    def _build_display_names(self, raw: list[str]) -> list[str]:
        out = []
        for name in raw:
            if not name:
                out.append("")
                continue
            label = clean_label(name)
            out.append(label if name_looks_valid(label) else "")
        return out

    def get_area_names(self) -> list[str]:
        """Raw name strings — one panel read, then cached for the session."""
        if self._area_names_raw is None:
            self._area_names_raw = self._resolve_names(
                self.num_areas,
                self._area_names_addr,
                PTR_NAMES_AREAS,
                DIRECT_AREA_NAMES,
            )
            self._area_names_display = self._build_display_names(self._area_names_raw)
        return self._area_names_raw

    def get_area_display_names(self) -> list[str]:
        """Trimmed labels for output; shares the same EEPROM read as get_area_names()."""
        if self._area_names_display is None:
            self.get_area_names()
        assert self._area_names_display is not None
        return self._area_names_display

    def get_zone_names(self, count: int) -> list[str]:
        if count not in self._zone_names_raw:
            self._zone_names_raw[count] = self._resolve_names(
                count, self._zone_names_addr, PTR_NAMES_ZONES, DIRECT_ZONE_NAMES,
            )
        return self._zone_names_raw[count]

    def get_scenario_names(self, count: int = SCENARIO_COUNT_MAX) -> list[str]:
        if self._scenario_names_raw is None:
            self._scenario_names_raw = self._resolve_names(
                count,
                self._scenario_names_addr,
                PTR_NAMES_SCENARIOS,
                DIRECT_SCENARIO_NAMES,
            )
        return self._scenario_names_raw[:count]

    def get_version(self) -> str:
        return self.read_memory(ADDR_VERSION, 13).decode("ascii", errors="replace").strip()

    def get_status(self, length: int | None = None) -> PanelStatus:
        if length is None:
            length = self.num_areas // 2 + 1 + 10
        raw = self.read_memory(ADDR_STATUS, length)
        return parse_status(raw, self.num_areas)

    def get_zone_status(self, num_zones: int) -> ZoneStatus:
        tr_len = (num_zones + 3) // 4 + 1
        z1_len = (num_zones + 7) // 8 + 1
        tr = self.read_memory(ADDR_STATUS_ZONE_TR, tr_len)
        z1 = self.read_memory(ADDR_STATUS_ZONE1, z1_len)
        return parse_zone_status(tr, z1, num_zones)

    def read_scenarios_config(
        self, addr: int, stride: int, count: int = SCENARIO_COUNT_MAX,
    ) -> list[dict]:
        cfg = self.read_memory(addr, stride * count)
        return parse_scenarios(cfg, stride, self.num_areas, count)

    def get_scenarios_state(
        self, addr: int, stride: int, count: int = SCENARIO_COUNT_MAX,
    ) -> list[dict]:
        scenarios = self.read_scenarios_config(addr, stride, count)
        status = self.get_status()
        for sc in scenarios:
            sc["active"] = scenario_is_active(sc["area_modes"], status)
        return scenarios

    def _send_panel_command(self, pin: str, area: int, mode: int) -> int:
        self._send(build_panel_command(pin, area, mode))
        ack = self._recv_exact(1)[0]
        self._send(build_read_frame(ADDR_ESITO, 2))
        confirm = self._recv_exact(2)
        self._log(f"confirm {confirm.hex()}")
        return ack

    def arm(self, pin: str, area: int, mode: str = "total") -> int:
        return self._send_panel_command(pin, area, ARM_MODES[mode])

    def disarm(self, pin: str, area: int) -> int:
        return self._send_panel_command(pin, area, MODE_DISARM)


def _status_names(client: InimClient, args: argparse.Namespace) -> list[str] | None:
    if args.no_names:
        return None
    return client.get_area_display_names()


def cmd_status(client: InimClient, args: argparse.Namespace) -> int:
    status = client.get_status()
    names = _status_names(client, args)

    if args.json:
        print(json.dumps(status_as_dict(status, names), indent=2))
    else:
        print(format_status(status, names))

    if args.verbose:
        print(f"\nraw ({len(status.raw)} B): {status.raw.hex()}", file=sys.stderr)
        print(hexdump(status.raw), file=sys.stderr)
    return 0


def cmd_version(client: InimClient, _: argparse.Namespace) -> int:
    print(client.get_version())
    return 0


def cmd_zones(client: InimClient, args: argparse.Namespace) -> int:
    zs = client.get_zone_status(args.zones)
    names = None if args.no_names else client.get_zone_names(args.zones)
    print(format_zone_status(zs, names))
    if args.verbose:
        print("TR:", zs.tr_zone.hex())
        print("Z1:", zs.zone1.hex())
    return 0


def cmd_scenarios(client: InimClient, args: argparse.Namespace) -> int:
    addrs = [args.addr] if args.addr is not None else list(SCENARIO_CFG_ADDRS)
    for addr in addrs:
        try:
            scenarios = client.get_scenarios_state(addr, args.stride)
        except (ConnectionError, TimeoutError, OSError) as exc:
            print(f"0x{addr:04X}: {exc}")
            continue
        if not scenarios:
            print(f"0x{addr:04X}: empty (wrong address?)")
            continue
        print(f"scenarios @ 0x{addr:04X}, stride {args.stride}")
        scen_names = client.get_scenario_names()
        for sc in scenarios:
            idx = sc["index"]
            label = scen_names[idx] if idx < len(scen_names) and scen_names[idx] else f"#{idx}"
            modes = ", ".join(
                f"A{a}={MODE_LABELS.get(m, m)}" for a, m in sc["area_modes"].items() if m
            )
            state = "active" if sc["active"] else "inactive"
            print(f"  {label} [{state}]  {modes}")
        return 0
    return 1


def _area_armed(status: PanelStatus, area: int) -> bool:
    return any(num == area and v is not None and is_armed(v) for num, v in status.areas)


def _area_disarmed(status: PanelStatus, area: int) -> bool:
    return any(num == area and v == MODE_DISARM for num, v in status.areas)


def cmd_arm(client: InimClient, args: argparse.Namespace) -> int:
    if not args.code:
        print("need --code or INIM_PIN", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"refusing to arm area {args.area} without --yes")
        return 1

    ack = client.arm(args.code, args.area, args.mode)
    print(f"ack: {ack_label(ack)}")
    time.sleep(0.5)
    status = client.get_status()
    print(format_status(status, client.get_area_display_names()))
    return 0 if _area_armed(status, args.area) else 1


def cmd_disarm(client: InimClient, args: argparse.Namespace) -> int:
    if not args.code:
        print("need --code or INIM_PIN", file=sys.stderr)
        return 2
    if not args.yes:
        print(f"refusing to disarm area {args.area} without --yes")
        return 1

    ack = client.disarm(args.code, args.area)
    print(f"ack: {ack_label(ack)}")
    time.sleep(0.5)
    status = client.get_status()
    print(format_status(status, client.get_area_display_names()))
    return 0 if _area_disarmed(status, args.area) else 1


COMMANDS = ("status", "version", "zones", "scenarios", "arm", "disarm")


def _host_typo_hint(host: str) -> str | None:
    """Catch '192.168.1.50disarm' — missing space before the subcommand."""
    for cmd in COMMANDS:
        if host.endswith(cmd) and len(host) > len(cmd):
            prefix = host[: -len(cmd)]
            if prefix and prefix[-1].isdigit():
                return f"host '{host}' looks like IP + '{cmd}' run together - try: --host {prefix} {cmd} ..."
    return None


def _check_argv_host(argv: list[str], parser: argparse.ArgumentParser) -> None:
    for i, tok in enumerate(argv):
        host: str | None = None
        if tok == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif tok.startswith("--host="):
            host = tok.split("=", 1)[1]
        if host and (hint := _host_typo_hint(host)):
            parser.error(hint)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="INIM SmartLiving TCP client (port 5004)",
        allow_abbrev=False,
    )
    p.add_argument("--host", default=os.environ.get("INIM_HOST"), help="panel IP (or INIM_HOST)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument(
        "--areas", type=int,
        default=int(os.environ.get("INIM_AREAS", str(DEFAULT_AREAS))),
    )
    p.add_argument("-v", "--verbose", action="store_true", help="hex dump on stderr")
    p.add_argument("--area-names-addr", type=lambda x: int(x, 0),
                   default=int(os.environ["INIM_AREA_NAMES_ADDR"], 0)
                   if os.environ.get("INIM_AREA_NAMES_ADDR") else None)
    p.add_argument("--zone-names-addr", type=lambda x: int(x, 0),
                   default=int(os.environ["INIM_ZONE_NAMES_ADDR"], 0)
                   if os.environ.get("INIM_ZONE_NAMES_ADDR") else None)
    p.add_argument("--scenario-names-addr", type=lambda x: int(x, 0),
                   default=int(os.environ["INIM_SCENARIO_NAMES_ADDR"], 0)
                   if os.environ.get("INIM_SCENARIO_NAMES_ADDR") else None)

    sub = p.add_subparsers(dest="command", required=True)

    st = sub.add_parser("status", help="read partition status")
    st.add_argument("--json", action="store_true", help="JSON output")
    st.add_argument("--no-names", action="store_true", help="skip name lookup")
    st.set_defaults(func=cmd_status)

    sub.add_parser("version", help="read firmware string").set_defaults(func=cmd_version)

    zn = sub.add_parser("zones", help="read zone inputs")
    zn.add_argument("--zones", type=int, default=10)
    zn.add_argument("--no-names", action="store_true")
    zn.set_defaults(func=cmd_zones)

    sc = sub.add_parser("scenarios", help="read ModoIns definitions")
    sc.add_argument("--addr", type=lambda x: int(x, 0), default=None)
    sc.add_argument("--stride", type=int, default=5)
    sc.set_defaults(func=cmd_scenarios)

    pin = "user PIN (or INIM_PIN)"
    arm = sub.add_parser("arm", help="arm one area")
    arm.add_argument("--mode", choices=sorted(ARM_MODES.keys()), default="total")
    arm.add_argument("--area", type=int, default=1)
    arm.add_argument("--code", default=os.environ.get("INIM_PIN"), help=pin)
    arm.add_argument("--yes", action="store_true")
    arm.set_defaults(func=cmd_arm)

    dis = sub.add_parser("disarm", help="disarm one area")
    dis.add_argument("--area", type=int, default=1)
    dis.add_argument("--code", default=os.environ.get("INIM_PIN"), help=pin)
    dis.add_argument("--yes", action="store_true")
    dis.set_defaults(func=cmd_disarm)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    argv = argv if argv is not None else sys.argv[1:]
    _check_argv_host(argv, parser)
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("need --host or INIM_HOST")

    try:
        with InimClient(
            args.host, args.port, verbose=args.verbose, num_areas=args.areas,
            area_names_addr=args.area_names_addr,
            zone_names_addr=args.zone_names_addr,
            scenario_names_addr=args.scenario_names_addr,
        ) as client:
            return args.func(client, args)
    except (ConnectionError, TimeoutError, OSError) as exc:
        print(f"connection failed: {exc}", file=sys.stderr)
        print("only one client at a time — close SmartLeague", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
