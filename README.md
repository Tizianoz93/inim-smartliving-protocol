# INIM SmartLiving TCP Protocol

Documentation and reference client for the TCP protocol between an **INIM SmartLiving** panel and its **SmartLAN/(SI/G)** network module (Ethernet, port **5004**).

> **Disclaimer:** Unofficial documentation — not an INIM specification. Use at your own risk.

## Quick start

```bash
cd examples
export INIM_HOST=192.168.1.50
export INIM_PIN=1234

python inim_client.py version
python inim_client.py status
python inim_client.py arm --mode away --area 1 --code $INIM_PIN --yes
```

The panel accepts **one TCP client at a time** — disconnect SmartLeague or other clients first.

Reference implementation: `examples/inim_client.py`. Memory layout and model-specific addresses: [docs/MEMORY_MAP.md](docs/MEMORY_MAP.md), [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md).

---

## Transport

| Parameter | Value |
|-----------|-------|
| Protocol | TCP |
| Port | **5004** (default) |
| Clients | **1** concurrent session |
| Encryption | Cleartext on typical LAN setups |

### Connection

```
Client                          Panel
  |---- TCP connect ------------>|
  |---- "pass" (4 ASCII bytes) ->|
  |     wait ~400 ms             |
  |---- read/write frames ------>|
  |<--- responses ---------------|
```

1. Open TCP to the panel IP on port 5004.
2. Send ASCII `pass` (`70 61 73 73`).
3. Wait **~400 ms** before the first protocol frame (required with `TCP_NODELAY`).
4. Exchange 8-byte read frames and 8+N-byte write frames.

---

## Wire format

### Read frame (8 bytes)

```
Byte:  0    1    2    3    4    5    6         7
       PH   PH   A2   A1   A0   00   LEN-1     CHECKSUM
```

| Field | Description |
|-------|-------------|
| Bytes 0–1 | Prefix: `00 00` = read |
| Bytes 2–4 | 24-bit **big-endian** memory address |
| Byte 5 | `00` |
| Byte 6 | Expected payload length **− 1** (max 255 → **256 bytes** per read) |
| Byte 7 | Sum of bytes 0–6 mod 256 |

**Response:** `payload` + 1 checksum byte (`sum(payload) mod 256`). The checksum is not part of the memory contents.

Larger regions must be read in **256-byte chunks** at consecutive addresses.

```python
def build_read_frame(address: int, length: int) -> bytes:
    body = bytearray(8)
    body[2:5] = address.to_bytes(3, "big")
    body[6] = (length - 1) & 0xFF
    body[7] = sum(body[:7]) & 0xFF
    return bytes(body)
```

### Write frame (8-byte header + payload)

Same header layout; prefix `01 00`. Byte 6 = payload length. Payload follows immediately.

Example — write 14 bytes to `@0x2006`:

```
01 00 00 20 06 00 0E 35  |  <14-byte payload>
```

### Command flow

After each write:

1. Panel sends a 1-byte ACK.
2. Client reads `@0x2004`, length 2.
3. Panel returns result (e.g. `01 01` = OK).

---

## Realtime memory (summary)

Fixed addresses on all models:

| Address | R/W | Purpose |
|---------|-----|---------|
| `0x2000` | read | Area status |
| `0x2001` | read | Zone terminal state |
| `0x2002` | read | Zone bypass / test |
| `0x2003` | read | Zone alarm memory + output status |
| `0x2004` | read | Command result |
| `0x2006` | write | Arm / disarm |
| `0x2007` | write | Output on / off |
| `0x4000` | read | Firmware version (ASCII) |

Poll `@0x2000`–`@0x2003` as **four separate reads**, not one long read from `0x2000`.

Full buffer layouts, EEPROM tables, and decode rules: [docs/MEMORY_MAP.md](docs/MEMORY_MAP.md).

---

## Area status and commands

### Status (`@0x2000`)

Two areas per byte (nibble-packed):

```python
def area_mode(data: bytes, area: int) -> int:
    p = area - 1
    b = data[p // 2]
    return (b & 0x0F) if p % 2 == 0 else (b >> 4) & 0x0F
```

| Nibble | Mode |
|--------|------|
| `1` | Away |
| `2` | Stay |
| `3` | Instant |
| `4` | Disarmed |
| `0` | Not configured |

Read length for `N` areas: `N // 2 + 1 + 10` bytes (515 with 5 areas → 14 bytes).

### Arm / disarm (`@0x2006`)

22 bytes total: 8-byte write header + 6-byte PIN + 8-byte area block.

**PIN:** one byte per digit; unused positions `0xFF`. PIN `1234` → `01 02 03 04 FF FF`.

**Area block:** same nibble layout as status. Set only the target area's nibble; others zero.

| Nibble | Command |
|--------|---------|
| `1` | Away |
| `2` | Stay |
| `3` | Instant |
| `4` | Disarm |

Area 1 Away, PIN 1234:

```
0100002006000e3501020304ffff0100000000000000
```

Multi-area writes set multiple nibbles (scenarios use this).

---

## Zone status

| Address | Encoding | Content |
|---------|----------|---------|
| `0x2001` | 2 bits/slot | 0=rest, 1=alarm, 2=short, 3=fault |
| `0x2002` | 1 bit/slot | Bypass |
| `0x2003` | 1 bit/slot | Alarm memory (zones); outputs follow |

Poll **slot** indices, not logical terminal indices directly. Double zones use two slots: `t×2+1` and `t×2+2` for logical terminal `t`. See [MEMORY_MAP.md](docs/MEMORY_MAP.md).

---

## Outputs

**Command** — write to `@0x2007`, 8-byte payload: PIN (6) + `[wire_index, state]` (`1`=on, `0`=off).

- BUS output: `wire_index` = physical terminal index.
- Onboard output: `wire_index` = physical index − **M** (515: RELE @20 → wire `0`).

**Status** — bit in `@0x2003` buffer at computed offset (depends on **M**). Same physical index for BUS; onboard indices start at **M**.

---

## Using the protocol

### Minimal integration

```
connect → pass → wait 400ms
loop:
  read @0x2000  (areas)
  read @0x2001  (zone states)
  read @0x2002  (bypass)
  read @0x2003  (alarms + outputs)
```

Trim read lengths to your panel's configured slot count. Suggested interval ≥ 0.5 s.

### Startup: labels and entity list

Configuration lives in EEPROM — addresses depend on model and firmware. Typical sequence (515 / 6.x):

1. Read `@0x4000` → identify firmware family.
2. Read configured-terminal bitmap `@0x14595` → active logical indices.
3. Read terminal programming `@0x14368` → `tipo_term` per index (zone vs output).
4. Map zones to poll slots; read Table A `@0x172F0` for names.
5. Read onboard count from `@0x1315F` byte 1 → names at `@0x17FA0`.

Details and decode rules: [MEMORY_MAP.md](docs/MEMORY_MAP.md). Model table and 515 example: [COMPATIBILITY.md](docs/COMPATIBILITY.md).

### Scenarios

Stored in EEPROM (nibble-packed target modes per area). No live status register — compare `modo[]` targets against `@0x2000`. Activate by writing `modo[]` to `@0x2006`.

### Names (firmware ≤ 5.x)

Read 2-byte LE pointer at `0x4014` / `0x4016` / `0x4024`, then `count × 16` bytes at the target address. On firmware 6.x use direct EEPROM addresses from COMPATIBILITY.

---

## Repository

```
inim-protocol/
├── README.md                 ← protocol and usage (this file)
├── docs/
│   ├── MEMORY_MAP.md         ← memory layout and decoding
│   └── COMPATIBILITY.md      ← models, firmware, EEPROM profiles
├── examples/
│   ├── inim_client.py        ← reference CLI client
│   └── alarm_proxy.py        ← TCP traffic logger (optional)
└── tools/
    └── dump_memory.py        ← bulk memory read to file
```

---

## Known gaps

- Zone include/exclude (`@0x2009`) payload format
- Scenario EEPROM address on all 6.x model variants (515: `@0x142BF`)
- EEPROM profile addresses for models other than 515 / fw 6.x
- Extended read prefix `10 00` for some high-memory blocks
- Encrypted links (AES not documented here)

---

*June 2026 — Tiziano Zorzo · [consulenza.app](https://consulenza.app) · Community documentation, not affiliated with INIM Electronics.*
