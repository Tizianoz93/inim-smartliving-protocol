#!/usr/bin/env python3
"""Sanity-check output wire index mapping against SmartLeague capture."""

from __future__ import annotations

M = 20

CASES = [
    (4, 4, "BUS USCITA DI PROVA"),
    (20, 0, "RELE' 001"),
    (21, 1, "USCITA 001"),
    (22, 2, "USCITA 002"),
]


def output_command_index(physical: int, num_terminals: int) -> int:
    if num_terminals and physical >= num_terminals:
        return physical - num_terminals
    return physical


def main() -> int:
    ok = True
    for phys, expected, label in CASES:
        got = output_command_index(phys, M)
        match = got == expected
        ok &= match
        print(f"  phys {phys:2d} -> wire {got:2d}  ({label})  {'OK' if match else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
