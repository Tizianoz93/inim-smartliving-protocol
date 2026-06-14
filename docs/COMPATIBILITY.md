# Model and firmware compatibility

## Core rule

**Realtime protocol** (status reads at `0x2000`–`0x2003`, commands at `0x2006`–`0x2009`) is **identical on every SmartLiving model.** Only **buffer sizes** and **EEPROM label addresses** change.

## Universal realtime addresses

| Address | Purpose | Access |
|---------|---------|--------|
| `0x2000` | Area status (nibble-packed) | read |
| `0x2001` | Zone terminal state | read |
| `0x2002` | Zone bypass/test | read |
| `0x2003` | Zone alarm/tamper + output status | read |
| `0x2004` | Command result | read |
| `0x2006` | Area arm/disarm | write |
| `0x2007` | Output command | write |
| `0x2008` | Area reset | write |
| `0x2009` | Zone include/exclude | write |
| `0x4000` | Firmware version (ASCII) | read |
| `0x4014` | Pointer → area names (fw ≤ 5.x) | read |
| `0x4016` | Pointer → zone names (fw ≤ 5.x) | read |
| `0x4024` | Pointer → scenario names (fw ≤ 5.x) | read |

### Polling

Use **four separate read frames** per cycle (`@0x2000` … `@0x2003`). Trim each response length to the highest configured zone poll slot and output index.

On a 515 with full trimmed reads, four round-trips take ~240 ms — use a scan interval ≥ 0.5–2 s in integrations.

---

## Model parameters

| Model | Areas | Terminals | Logical terminals (M) | Outputs | Scenarios | Scenario stride | `modo[]` bytes |
|-------|-------|-----------|-----------------------|---------|-----------|-----------------|----------------|
| SmartLiving **505** | 5 | 5 | 10 | 10 | 30 | 5 | 3 |
| SmartLiving **515** | 5 | 15 | 20 | 20 | 30 | 5 | 3 |
| SmartLiving **1050** (incl. /G3, L, L/G) | 10 | 50 | 50 | 48* | 30 | 8 | 6 |
| SmartLiving **10100L / 10100L/G3** | 15 | 100 | 100 | 48* | 30 | 10 | 8 |

Notes:

- **Terminals** — mappable count from the datasheet; size zone buffers to configured slots, not the full M×2 table.
- **M (logical terminals)** — sizes realtime buffers and the output-bit offset in `@0x2003`; can exceed mappable terminal count.
- **Outputs** — upper bound 48 on all models; actual count depends on hardware.
- **Scenario `modo[]`** — nibble-packed width = `areas // 2 + 1` bytes.

---

## Firmware families

Read `@0x4000` first. The first three characters select the name-table method:

| Firmware prefix | Name lookup |
|-----------------|-------------|
| `1.0` | Pointer-based (legacy) |
| `2.0`–`6.0` | Pointer-based or direct EEPROM (6.x: pointers often invalid) |

Example: `6.09 00515` → family 6.x, 515 model code in suffix.

### Scenario config address (`eep_prg_mod_ins`)

| Firmware | Address |
|----------|---------|
| 1.x | `0x1E58`, `0x376A`, `0x625A` (model-dependent) |
| 5.x | `0x2ED5`, `0x3572`, `0x5F9C`, `0xA9F2` |
| 6.x | `@0x142BF` on 515; other variants may differ |

---

## EEPROM profile: 515 / firmware 6.x

| Block | Address | Format |
|-------|---------|--------|
| Area names | `0x172A0` | 16 B/entry |
| Table A (zone/output names) | `0x172F0` | 16 B × M |
| Table B (physical labels) | `0x17430` | 16 B × M |
| Terminal programming | `0x14368` | 12 B × M |
| Configured-terminal bitmap | `0x14595` | 67 B |
| Onboard output summary | `0x1315F` | 7 B (byte 1 = count) |
| Onboard output enable | `0x154A8` | 24 B |
| Onboard output names | `0x17FA0` | 16 B × count |
| Scenario names | `0x17CF0` | 16 B × 30 |
| Scenario config | `0x142BF` | stride 5 |

Live reads for terminal programming may use `@0x12F07` (chunked); offline dumps align at `@0x14368`.

---

## Example: 515 / 6.09 entity table

| Logical idx | Table A | `tipo_term` | In bitmap | Role |
|-------------|---------|-------------|-----------|------|
| 0 | Finestra came2 | 0 | yes | zone → poll slot 1 |
| 1 | FIN BAGNO | 0 | yes | zone → poll slot 3 |
| 2 | PIR_BALCONE | 0 | yes | zone → slots 5, 6 (double) |
| 3 | PIR CAMERA 1 | 0 | yes | zone |
| 4 | USCITA DI PROVA | 1 | yes | BUS output, phys idx 4 |
| 8 | DT SALA | 0 | yes | zone (keypad input) |
| 9 | Centrale T10 | 0 | **no** | placeholder — ignore |

Bitmap bytes: `0, 1, 2, 3, 4, 8`. Onboard count `@0x1315F` byte 1 = `3` → indices 20–22 (`RELE' 001`, `USCITA 001`, `USCITA 002`).

### Table B examples

| idx | Table B |
|-----|---------|
| 0 | Centrale T01 |
| 1 | Espans. 01 T01 |
| 2 | AM_BALCONE |
| 4 | Espans. 02 T05 |
| 8 | Tast. 01 T01 |

### Terminal display ID (optional)

Computed from Table B physical label, not stored as an integer:

| Location | Formula |
|----------|---------|
| Panel `Tx` | `x` |
| Expansion `N` `Ty` | `10 + (N−1)×5 + y` |
| Keypad `Ty` | `60 + y` |
| Double zone, 2nd half | `primary + 500` |

Re-read tables after installer reprogramming — indices shift.

---

## Output index mapping (515)

| Index range | Status (`@0x2003`) | Command (`@0x2007`) | Name |
|-------------|--------------------|---------------------|------|
| `0 … M−1` | physical index | same (BUS) | Table A |
| `M …` | physical index | wire = `idx − M` (onboard) | `@0x17FA0` |

Example: RELE @ phys 20 → command wire `0`, status bit at offset 9 + index 20.

---

## Name record byte 15

| Address | Name | Byte 15 |
|---------|------|---------|
| `0x178F0` | MARA | `0x7B` flag |
| `0x175F0` | CODICE 009 | `0x30` flag |
| `0x180F0` | EV. PROG. 001 | `0xFD` flag |
| `0x17FA0` | RELE' 001 | `0x20` padding |

When byte 14 is still padding, byte 15 is a flag — exclude it from the displayed label.

---

## Scenarios

- Labels: installer-defined, 16-byte slots at scenario name address.
- Config: `modo[]` per slot — nibble-packed target mode per area (`0` = not included).
- **Active:** all included areas match `@0x2000` live modes.
- **Activate:** write `modo[]` to `@0x2006`.

No fixed ON/OFF/partial enum at the protocol level.

---

## Integration checklist

1. Connect, send `pass`, wait ~400 ms.
2. Read `@0x4000` → firmware and model hint.
3. Set **M**, area count, EEPROM profile from tables above.
4. **Poll loop:** four reads at `@0x2000`–`@0x2003` (trimmed lengths).
5. **Once at startup:** bitmap → programming → names (see [MEMORY_MAP.md](MEMORY_MAP.md)).
6. **Commands:** arm/disarm `@0x2006`, outputs `@0x2007`, then poll `@0x2004`.
7. **Scenarios:** read config + names; derive active state from area status.

---

## Protocol limits

- **One TCP client** at a time.
- **256 bytes** max per read (chunk larger EEPROM blocks).
- **Handshake delay** ~400 ms after `pass` before first frame.
