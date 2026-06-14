#!/usr/bin/env python3
"""Benchmark INIM realtime poll strategies (515 / fw 6.x).

  set INIM_HOST=192.168.1.121
  python benchmark_poll.py

Compares round-trip count and latency for area + zone + output reads.
"""

from __future__ import annotations

import argparse
import os
import socket
import statistics
import sys
import time
from dataclasses import dataclass

DEFAULT_HOST = os.environ.get("INIM_HOST", "192.168.1.121")
DEFAULT_PORT = int(os.environ.get("INIM_PORT", "5004"))
HANDSHAKE_DELAY = 0.4
IO_TIMEOUT = 8.0

ADDR_AREAS = 0x002000
ADDR_TR = 0x002001
ADDR_Z1 = 0x002002
ADDR_Z2 = 0x002003

M = 20
NUM_AREAS = 5
# Reference panel configured zone poll indices
ZONE_POLLS = [1, 3, 5, 6, 7, 17]
OUTPUT_IDX = [4, 20, 21, 22]


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


def tr_full(num_terminals: int) -> int:
    return (num_terminals * 2 * 2 + 7) // 8 + 1


def z1_full(num_terminals: int) -> int:
    section = (num_terminals * 2 + 7) // 8 + 1
    return section + section


def tr_min(max_poll: int) -> int:
    return (max_poll - 1) // 4 + 1


def z1_min(max_poll: int) -> int:
    return (max_poll - 1) // 8 + 1


def z2_min(num_terminals: int, outputs: list[int]) -> int:
    if not outputs:
        return 0
    return (num_terminals * 2 // 8 + 1) + (num_terminals // 8 + 1) + max(outputs) // 8 + 1


def z2_smartleague_515() -> int:
    """Wire payload bytes observed in SmartLeague idle poll (@0x2003, 14 B)."""
    return 14


@dataclass
class Strategy:
    name: str
    reads: list[tuple[int, int]]  # (address, data_bytes wanted)


STRATEGIES = [
    Strategy(
        "ha_current (4× full)",
        [
            (ADDR_AREAS, area_len(NUM_AREAS)),
            (ADDR_TR, tr_full(M)),
            (ADDR_Z1, z1_full(M)),
            (ADDR_Z2, z2_min(M, OUTPUT_IDX)),
        ],
    ),
    Strategy(
        "smartleague_sizes (4×)",
        [
            (ADDR_AREAS, 14),
            (ADDR_TR, tr_full(M)),
            (ADDR_Z1, z1_full(M)),
            (ADDR_Z2, z2_smartleague_515()),
        ],
    ),
    Strategy(
        "minimal_separate (4× trimmed)",
        [
            (ADDR_AREAS, area_len(NUM_AREAS)),
            (ADDR_TR, tr_min(max(ZONE_POLLS))),
            (ADDR_Z1, z1_min(max(ZONE_POLLS))),
            (ADDR_Z2, z2_min(M, OUTPUT_IDX)),
        ],
    ),
    Strategy(
        "block_0x2000 full",
        [(
            ADDR_AREAS,
            area_len(NUM_AREAS) + tr_full(M) + z1_full(M) + z2_min(M, OUTPUT_IDX),
        )],
    ),
    Strategy(
        "block_0x2000 smartleague",
        [(
            ADDR_AREAS,
            14 + tr_full(M) + z1_full(M) + z2_smartleague_515(),
        )],
    ),
    Strategy(
        "block_0x2000 minimal",
        [(
            ADDR_AREAS,
            area_len(NUM_AREAS)
            + tr_min(max(ZONE_POLLS))
            + z1_min(max(ZONE_POLLS))
            + z2_min(M, OUTPUT_IDX),
        )],
    ),
    Strategy(
        "areas+zones only (3× min, no z2)",
        [
            (ADDR_AREAS, area_len(NUM_AREAS)),
            (ADDR_TR, tr_min(max(ZONE_POLLS))),
            (ADDR_Z1, z1_min(max(ZONE_POLLS))),
        ],
    ),
]


class Panel:
    def __init__(self, host: str, port: int) -> None:
        self.sock: socket.socket | None = None
        self.host = host
        self.port = port

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=IO_TIMEOUT)
        self.sock.settimeout(IO_TIMEOUT)
        self.sock.sendall(b"pass")
        time.sleep(HANDSHAKE_DELAY)

    def close(self) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def read_at(self, address: int, length: int) -> bytes:
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

    def run_strategy(self, strat: Strategy) -> tuple[float, int]:
        t0 = time.perf_counter()
        total_bytes = 0
        for addr, nbytes in strat.reads:
            total_bytes += len(self.read_at(addr, nbytes))
        elapsed = time.perf_counter() - t0
        return elapsed, total_bytes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    args = ap.parse_args()

    panel = Panel(args.host, args.port)
    try:
        print(f"Connect {args.host}:{args.port} …")
        panel.connect()
        fw = panel.read_at(0x004000, 12).decode("ascii", errors="replace").strip()
        print(f"Firmware: {fw}\n")
        print(f"Zone polls: {ZONE_POLLS}  |  Outputs: {OUTPUT_IDX}\n")
        print(f"{'Strategy':<32} {'RT':>3} {'bytes':>6} {'avg ms':>8} {'p95 ms':>8} {'min ms':>8}")
        print("-" * 72)

        results: list[tuple[Strategy, list[float]]] = []
        for strat in STRATEGIES:
            for _ in range(args.warmup):
                panel.run_strategy(strat)
            times: list[float] = []
            for _ in range(args.rounds):
                ms, nbytes = panel.run_strategy(strat)
                times.append(ms * 1000)
            results.append((strat, times))
            times_sorted = sorted(times)
            p95 = times_sorted[int(len(times_sorted) * 0.95) - 1]
            print(
                f"{strat.name:<32} {len(strat.reads):>3} "
                f"{nbytes:>6} {statistics.mean(times):>8.1f} "
                f"{p95:>8.1f} {min(times):>8.1f}"
            )

        best = min(results, key=lambda x: statistics.mean(x[1]))
        avg_ms = statistics.mean(best[1])
        print(f"\nFastest: {best[0].name} (~{avg_ms:.0f} ms/cycle)")
        suggested = max(0.5, round(avg_ms * 3 / 1000, 1))
        print(
            f"Suggested HA scan_interval: {suggested}s "
            f"(~3× poll latency; min 0.5s)"
        )
    except (OSError, TimeoutError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    finally:
        panel.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
