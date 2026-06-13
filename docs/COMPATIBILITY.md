# Model and firmware compatibility

Analysis from reverse engineering `Centrale5.dll` (`InizializzaRam_*`, `InizializzaVariabili_*`, `LeggiDescrizioni*`).

## Key finding

> **Real-time protocol (status reads and arm/disarm commands) is identical across all SmartLiving models and firmware revisions.** Only **dimensions** (area/zone/scenario counts) and **EEPROM configuration addresses** differ.

The following addresses are assigned the **same value** in every `InizializzaRam_1_00` … `_6_00` variant:

| Address | Purpose | Access |
|---------|---------|--------|
| `0x2000` | Area status (nibble-packed) | read |
| `0x2001` | Zone terminal state | read |
| `0x2002` | Zone bypass/test | read |
| `0x2003` | Zone alarm/tamper memory | read |
| `0x2004` | Command result | read |
| `0x2006` | Area arm/disarm command | write |
| `0x2007` | Output command | write |
| `0x2008` | Area reset | write |
| `0x2009` | Zone include/exclude | write |
| `0x4000` | Firmware version (ASCII) | read |
| `0x4014` | **Pointer** to area names (fw ≤ 5.x) | read |
| `0x4016` | **Pointer** to zone names (fw ≤ 5.x) | read |
| `0x4024` | **Pointer** to scenario names (fw ≤ 5.x) | read |

### Name format (all models)

Two-step read on firmware ≤ 5.x:

1. Read 2-byte little-endian pointer at `k_sw_str_*`
2. Read `N × 16` bytes from the pointed address

Each name is a **16-byte** CP1252/Latin-1 string, space-padded, terminated with `0x00` or `0xFF`.

## Model-dependent parameters

| Model | Areas | Max zones | Real zones | Scenarios | Scenario stride | `modo[]` bytes |
|-------|-------|-----------|------------|-----------|-----------------|----------------|
| SmartLiving **505** | 5 | 10 | 5 | 30 | 5 | 3 |
| SmartLiving **515** | 5 | 20 | 15 | 30 | 5 | 3 |
| SmartLiving **1050** | 10 | 50 | 50 | 30 | 8 | 6 |
| SmartLiving **10100 / Prime** | 15 | 100 | 100 | 30 | 10 | 8 |

Notes:

- “Max zones” includes doubling; use this count when reading zone status buffers.
- Up to 30 scenario slots exist; typically only a few are programmed.
- Scenario `modo[]` is nibble-packed: `bytes = MAX_PARTITIONS / 2 + 1`.

## Scenario EEPROM address (`eep_prg_mod_ins`)

Variable by firmware family and sub-model:

| Firmware family | Observed values |
|-----------------|-----------------|
| 1.x | `0x1E58`, `0x376A`, `0x625A` |
| 5.x | `0x2ED5`, `0x3572`, `0x5F9C`, `0xA9F2` |
| 6.x | Loaded at runtime from internal config keys (`cinque_*`, `quindici_*`, …) |

On firmware 6.x, SmartLeague resolves addresses from embedded resources — not from the fixed pointers above.

## Firmware variant selection

`Centrale5.dll` maps the first three characters of the firmware version:

| Firmware prefix | Method variant |
|-----------------|----------------|
| `1.0` | `*10` (legacy format) |
| `2.0`–`6.0` | `*20` (extended format) |

Example firmware string `6.09 00515` → family **6.x**, extended format.

## Field-tested notes (515 / firmware 6.09)

### Handshake timing

After sending `pass`, wait **~400 ms** before the first frame. Without delay, TCP may coalesce `pass` with the first read and the panel will not respond. Use `TCP_NODELAY` + explicit delay in async clients.

### Read chunking

Response length field is 1 byte → **maximum 256 bytes per read**. Chunk larger reads (zone/scenario name blocks) into consecutive 256-byte requests.

### Direct name addresses (515 / 6.09)

On firmware 6.x, pointers at `0x4014`/`0x4016`/`0x4024` return invalid data. Verified direct addresses:

| Block | Address | Format |
|-------|---------|--------|
| Area names | `0x172A0` | 16 bytes/entry |
| Zone names | `0x172F0` | 16 bytes/entry |
| Scenario names | `0x17CF0` | 16 bytes/entry, 30 slots |
| Scenario config | `0x142BF` | stride 5, 3-byte nibble-packed `modo[]` |

### Scenario names and configuration

Scenario labels are **installer-defined** strings (16-byte ASCII slots, same encoding as area/zone names). The protocol does not define fixed names such as “on”, “off”, or “partial” — only whatever is stored in the scenario name block.

Each programmed slot holds a **target arm mode per area** in `modo[]` (nibble-packed; see model table for width). Areas with mode `0` (not present) are skipped. A slot is only meaningful if at least one area has a non-zero target.

Typical patterns (labels and area count vary by installation):

| Slot | Label source | `modo[]` pattern |
|------|--------------|------------------|
| 0 | EEPROM name block | all configured areas → same mode (often Away) |
| 1 | EEPROM name block | mixed targets (e.g. Stay on one area, Away on another) |
| 2 | EEPROM name block | all configured areas → Disarm |

Read the name block and `eep_prg_mod_ins` entry together; neither implies a fixed semantic by index.

### Scenario active state

There is **no dedicated scenario status register**. Activity is **derived**: a slot is **active** when every included area’s live mode at `0x2000` matches that slot’s `modo[]` target; otherwise **inactive**. This is a boolean at the wire level — any “partial” behaviour is either a user-assigned label or a mix of per-area modes, not a separate runtime enum.

## Integration strategy

1. **Universal core:** fixed addresses `0x2000` / `0x2006` for all models.
2. **Parametric sizing:** area/zone/scenario counts from model table or discovery.
3. **Names:** pointer method (fw ≤ 5.x) or address profile table (fw 6.x).
4. **Scenarios:** derive active/inactive from area status; EEPROM config needed only for definitions and activation.
