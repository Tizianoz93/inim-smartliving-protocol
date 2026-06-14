# Memory map

The panel exposes a flat **24-bit address space** over TCP. Reads and writes target specific addresses; there is no file system or register file abstraction on the wire.

## Address classes

| Range (typical) | Role |
|-----------------|------|
| `0x2000`–`0x2009` | **Realtime RAM** — live status and command targets (same addresses on all models) |
| `0x4000`+ | **Configuration / labels** — firmware string, name tables, programming data (addresses vary by model and firmware) |

Realtime addresses are **fixed across all SmartLiving models**. Buffer **lengths** and EEPROM **base addresses** depend on the panel — see [COMPATIBILITY.md](COMPATIBILITY.md).

---

## Realtime map

| Address | R/W | Content |
|---------|-----|---------|
| `0x2000` | read | Area status (nibble-packed) + alarm bitmaps |
| `0x2001` | read | Zone terminal state (2 bits per poll slot) |
| `0x2002` | read | Zone bypass / test (1 bit per poll slot) |
| `0x2003` | read | Zone alarm/tamper memory + **output status bitmap** |
| `0x2004` | read | Last command result |
| `0x2006` | write | Arm/disarm (PIN + nibble-packed area block) |
| `0x2007` | write | Output on/off (PIN + wire index + state) |
| `0x2008` | write | Area reset |
| `0x2009` | write | Zone include/exclude |
| `0x4000` | read | Firmware version (ASCII, ~13 bytes) |

### Important: separate read targets

`0x2000`–`0x2003` are **independent read addresses**, not consecutive bytes of one block. A long read starting at `@0x2000` returns correct area data but **incorrect** zone and output slices. Poll each address with its own 8-byte read frame.

---

## Area status (`@0x2000`)

**Layout:** nibble-packed modes — 2 areas per byte, then 1-bit-per-area alarm bitmaps.

Partition `p = area − 1`:

```python
byte_idx = p // 2
nibble = (data[byte_idx] & 0x0F) if p % 2 == 0 else (data[byte_idx] >> 4) & 0x0F
```

| Nibble | Mode |
|--------|------|
| `0` | Not configured |
| `1` | Armed Away |
| `2` | Armed Stay |
| `3` | Armed Instant |
| `4` | Disarmed |

**Read length** for `N` areas: `N // 2 + 1 + 10` bytes (includes alarm regions). Example: 5 areas → 14 bytes.

Command writes to `@0x2006` use the **same nibble encoding** in an 8-byte area block.

---

## Zone status (`@0x2001`–`@0x2003`)

| Address | Bits | Meaning |
|---------|------|---------|
| `0x2001` | 2 per slot | Terminal state: 0=rest, 1=alarm, 2=short, 3=fault |
| `0x2002` | 1 per slot | Bypass / test |
| `0x2003` | 1 per slot | Alarm/tamper memory (zones); output bits follow (see below) |

```python
def zone_state(tr_zone: bytes, slot: int) -> int:  # slot 1-based
    z = slot - 1
    return (tr_zone[z // 4] >> ((z % 4) * 2)) & 0x03

def zone_bypass(zone1: bytes, slot: int) -> bool:
    z = slot - 1
    return bool((zone1[z // 8] >> (z % 8)) & 0x01)
```

**Poll slot count:** the buffer at `@0x2001` holds **M×2** slots (not M), where **M** = logical terminal count for the model. Double-balanced zones consume two consecutive slots per logical terminal:

| Logical terminal index `t` | Primary poll slot | Double half |
|----------------------------|-------------------|-------------|
| `t` | `t × 2 + 1` | `t × 2 + 2` |

Example (515, M=20): terminal idx 2 → poll slots **5** and **6**.

**Buffer sizes** (approximate, trim to highest configured slot):

- `@0x2001`: `(slots + 3) // 4 + 1` bytes
- `@0x2002`, `@0x2003`: `(slots + 7) // 8 + 1` bytes minimum for zone portion

Unconfigured slots often read as `0x55` patterns.

---

## Output status (`@0x2003`)

No dedicated output register. Output bits are appended inside the `@0x2003` buffer, after zone alarm-memory and tamper-memory regions:

```text
offset = (M * 2 // 8 + 1) + (M // 8 + 1)
on = bit(buffer[offset + idx // 8], idx % 8)
```

- Indices `0 … M−1`: BUS terminals (zones and BUS outputs share this space).
- Indices `M …`: onboard outputs (RELE, open-collector, …).

**M** by model: 505→10, 515→20, 1050→50, 10100→100.

---

## Command result (`@0x2004`)

After a write, read 2 bytes. `01 01` indicates success. Poll once per command.

---

## Configuration EEPROM (515 / firmware 6.x)

Direct addresses when pointer reads at `0x4014`/`0x4016`/`0x4024` are invalid (firmware 6.x):

| Block | Address | Format |
|-------|---------|--------|
| Area names | `0x172A0` | 16 B × area count |
| **Table A** — zone/output names | `0x172F0` | 16 B × **M** |
| **Table B** — physical labels | `0x17430` | 16 B × **M** |
| Terminal programming | `0x14368` | 12 B × **M**; byte 11 = `tipo_term` |
| Configured-terminal bitmap | `0x14595` | 67 B |
| Onboard output summary | `0x1315F` | 7 B; **byte 1** = onboard output count |
| Onboard output enable | `0x154A8` | 24 B |
| Onboard output names | `0x17FA0` | 16 B × count |
| Scenario names | `0x17CF0` | 16 B × 30 |
| Scenario config | `0x142BF` | stride 5; 3-byte nibble-packed `modo[]` |

### Name records (16 bytes)

Space-padded ASCII/CP1252. Byte 15 may be a type flag, not part of the label:

```text
if byte[15] in (0x20, 0x00, 0xFF):   label = bytes[0:16]
elif byte[14] in (0x20, 0x00, 0xFF): label = bytes[0:15]
else:                                label = bytes[0:16]
```

### Table A vs Table B

Both indexed by **logical terminal index** (0 … M−1):

- **Table A** (`@0x172F0`): user-facing name (zone or output).
- **Table B** (`@0x17430`): physical position (`Centrale T01`, `Espans. 01 T05`, …) — or the **partner name** for a double zone.

### `tipo_term` (byte 11 of 12-byte programming record)

| Value | Type |
|-------|------|
| `0` | Zone (input) |
| `1` | BUS output |
| `2` | Mixed IO (keypad) |
| `3` | Double-balanced zone |

### Configured-terminal bitmap (`@0x14595`)

67-byte block. Each non-`0xFF` byte (except the trailing checksum byte) is a **logical terminal index** active in the installation. Placeholder slots (e.g. `Centrale T10`) may have names in Table A but **not** appear in this bitmap.

Example (515): indices `0, 1, 2, 3, 4, 8` → five zones + one BUS output.

### Output names

| Physical index | Name source |
|----------------|-------------|
| `idx < M` | Table A: `@0x172F0 + idx × 16` |
| `idx ≥ M` | Onboard: `@0x17FA0 + (idx − M) × 16` |

Onboard count comes from byte 1 of `@0x1315F`; read only that many slots from `@0x17FA0`.

---

## Scenarios

No realtime status register. Each scenario stores a nibble-packed **`modo[]`** (same encoding as arm commands) in EEPROM at `eep_prg_mod_ins` — address varies by firmware; `@0x142BF` on 515 / 6.x.

**Active** = every area with non-zero target in `modo[]` currently matches live mode at `@0x2000`.

**Activate** = write `modo[]` nibbles as an area command to `@0x2006`.

Record stride by area count: 5 areas → 5 bytes/record (3 bytes `modo[]`); see [COMPATIBILITY.md](COMPATIBILITY.md).

---

## Reconstructing the entity list

To build zones, outputs, and labels for an integration:

```
1. Read firmware @0x4000          → model hint, firmware family
2. Select M and EEPROM addresses  → COMPATIBILITY.md
3. Read bitmap @0x14595          → active logical indices
4. Read programming @0x14368      → tipo_term per index
5. For each bitmap index:
     tipo 0 or 3 → zone; poll slots t×2+1 (+ t×2+2 if tipo 3)
     tipo 1      → BUS output; physical index = logical index
6. Read @0x1315F byte 1 = N      → onboard output count
7. Read N names @0x17FA0          → onboard outputs at indices M … M+N−1
8. Read Table A/B as needed       → display names
```

Fallback when bitmap address is unknown (firmware ≤ 5.x): filter placeholder names (`Centrale Txx`, `SIRENA NNN`, …) combined with `tipo_term`.

---

## Name lookup (firmware ≤ 5.x)

Two-step read:

1. Read 2-byte little-endian pointer at `0x4014` (areas), `0x4016` (zones), or `0x4024` (scenarios).
2. Read `count × 16` bytes from the pointed address.

---

## Related

- [README.md](../README.md) — wire format and commands
- [COMPATIBILITY.md](COMPATIBILITY.md) — model parameters and address profiles
