# INIM SmartLiving TCP Protocol (Proof of Concept)

Unofficial documentation and reference implementation for the proprietary TCP protocol used by **INIM SmartLeague** to communicate with **INIM SmartLiving** alarm panels over a **SmartLAN/(SI/G)** network module (Ethernet, default port **5004**).

> **Disclaimer:** This is reverse-engineered work, not an official INIM specification. Use at your own risk.

## Reference hardware used during development


| Property | Example value                           |
| -------- | --------------------------------------- |
| Model    | SmartLiving **515** (5 areas, 20 zones) |
| Firmware | **6.09 00515**                          |


Methodology: transparent TCP proxy captures + IL decompilation of SmartLeague `Centrale5.dll` (`EseguiComando`, `AreeRealTime`, `ZoneRealTime`, `InizializzaRam_`*). Generated frames match SmartLeague byte-for-byte.

## Quick start

```bash
cd examples
export INIM_HOST=192.168.1.50   # your panel IP
export INIM_PIN=1234            # your user PIN (for arm/disarm only)

python inim_client.py version
python inim_client.py status
python inim_client.py --areas 5 zones --zones 20
python inim_client.py scenarios --stride 5 --addr 0x142BF
python inim_client.py arm --mode away --area 1 --code $INIM_PIN --yes
python inim_client.py disarm --area 1 --code $INIM_PIN --yes
```

**Important:** the panel accepts **one TCP client at a time**. Close SmartLeague before using the reference client.

### Sniffing with the proxy

```bash
cd examples
python alarm_proxy.py --target 192.168.1.50
```

Point SmartLeague at the proxy host (port 5004). Logs are written to `capture_logs/`.

## Transport


| Parameter              | Value                                                           |
| ---------------------- | --------------------------------------------------------------- |
| Protocol               | TCP                                                             |
| Default port           | **5004**                                                        |
| Concurrent connections | **1**                                                           |
| Encryption             | Not observed on local SmartLeague LAN (native DLL supports AES) |


## Connection sequence

```
Client                          Panel
  |---- TCP connect ------------>|
  |---- "pass" (4 ASCII bytes) ->|   mandatory handshake
  |     (wait ~0.4 s)            |
  |---- 8/22-byte frames ------->|
  |<--- responses ---------------|
```

After TCP connect, send the ASCII string `pass` (`70 61 73 73`). **Wait ~400 ms** before the first protocol frame. Without this delay (especially with `TCP_NODELAY`), `pass` and the first read frame may be coalesced and the panel will not respond.

## Message types

1. **Read frame** — 8 bytes (memory poll / status)
2. **Write frame** — 8-byte header + payload (area arm/disarm, outputs, zones)
3. **Area command** — write to `0x2006` with 6-byte PIN + 8-byte nibble-packed area block (14-byte payload, 22 bytes total)

## Read frame (8 bytes)

```
Byte:  0    1    2    3    4    5    6         7
       PH   PH   A2   A1   A0   00   LEN-1     CHECKSUM
```


| Field      | Description                                                                |
| ---------- | -------------------------------------------------------------------------- |
| `PH` (0–1) | Prefix: `00 00` = standard read, `01 00` = write (on write frames)         |
| `A2 A1 A0` | 24-bit big-endian memory address                                           |
| Byte 5     | Always `00` in observed samples                                            |
| Byte 6     | **Expected response length − 1** (max **255** → **256 bytes per request**) |
| Byte 7     | Checksum = sum of bytes 0–6 mod 256                                        |


Larger reads must be **chunked** into 256-byte requests at consecutive addresses.

```python
def build_read_frame(address: int, response_len: int, prefix: int = 0) -> bytes:
    body = bytearray(8)
    body[0] = prefix & 0xFF
    body[1] = (prefix >> 8) & 0xFF
    body[2] = (address >> 16) & 0xFF
    body[3] = (address >> 8) & 0xFF
    body[4] = address & 0xFF
    body[5] = 0x00
    body[6] = (response_len - 1) & 0xFF
    body[7] = sum(body[:7]) & 0xFF
    return bytes(body)
```

## Memory map (real-time, model-independent)

These addresses are identical across SmartLiving models and firmware families (`InizializzaRam_1_00` … `_6_00`):


| Address  | DLL field / constant           | Access | Purpose                             |
| -------- | ------------------------------ | ------ | ----------------------------------- |
| `0x2000` | `com_read_stato_area`          | read   | **Area status** (nibble-packed)     |
| `0x2001` | `com_read_tr_zone`             | read   | Zone terminal state (2 bit/zone)    |
| `0x2002` | `com_read_stato_zone1`         | read   | Zone bypass / test (1 bit/zone)     |
| `0x2003` | `com_read_stato_zone2`         | read   | Zone alarm/tamper memory            |
| `0x2004` | `CMD_READ_ESITO_COM_PC`        | read   | Command result / confirmation       |
| `0x2006` | `CMD_AREA_INSERIMENTI_ADDRESS` | write  | **Arm/disarm areas**                |
| `0x2007` | `CMD_USCITA_ATTDIS_ADDRESS`    | write  | Outputs on/off                      |
| `0x2008` | `CMD_AREA_RESETAREA_ADDRESS`   | write  | Area reset (bitmap)                 |
| `0x2009` | `CMD_ZONA_INCESC_ADDRESS`      | write  | Zone include/exclude                |
| `0x4000` | —                              | read   | Firmware version (ASCII, ~13 bytes) |


Name pointers (firmware ≤ 5.x, two-step read):


| Pointer addr | Content                            |
| ------------ | ---------------------------------- |
| `0x4014`     | 2-byte LE pointer → area names     |
| `0x4016`     | 2-byte LE pointer → zone names     |
| `0x4024`     | 2-byte LE pointer → scenario names |


Each name is **16 bytes**, space-padded ASCII/CP1252. On **firmware 6.x** these pointers are invalid; use direct addresses from [COMPATIBILITY.md](docs/COMPATIBILITY.md).

## Area status (`0x2000`)

Decoded in `Centrale5.dll::AreeRealTime`. Status is **nibble-packed: 2 areas per byte**.

For partition `p = area − 1`:

- byte index = `p // 2`
- **low nibble** if `p` is even, **high nibble** if odd

```python
def area_nibble(data: bytes, area: int) -> int:
    p = area - 1
    b = data[p // 2]
    return (b & 0x0F) if p % 2 == 0 else (b >> 4) & 0x0F
```

### Nibble values (mode)


| Nibble | Meaning                  |
| ------ | ------------------------ |
| `0`    | Area not configured      |
| `1`    | Armed **Away** (total)   |
| `2`    | Armed **Stay** (partial) |
| `3`    | Armed **Instant**        |
| `4`    | **Disarmed**             |


### Examples (5-area panel)

```
44 44 44 …  → areas 1–5 all disarmed (4+4 in each byte)
44 44 41 …  → area 5 low nibble = 1 (Away), area 6 slot = 4
44 14 44 …  → area 4 high nibble = 1 (Away)
```

> **Correction:** `0x44` encodes **two** disarmed areas (nibbles 4+4), not a single ASCII `'D'`.

After the status region, separate **1-bit-per-area** bitmaps follow for live alarm and alarm memory (`AreeRealTime`).

Typical read length: `MAX_PARTITIONS // 2 + 1 + 10` → **14 bytes** for 5 areas.

## Area command frame (22 bytes)

Write to `0x2006` via `Centrale5.dll::EseguiComando`.

```
Offset  Len  Field
0       8    Write header
8       6    User PIN (6 bytes, 0xFF padding)
14      8    Area block (nibble-packed modes)
```

### Write header example

```
01 00 00 20 06 00 0E 35
```


| Byte | Value      | Meaning                                   |
| ---- | ---------- | ----------------------------------------- |
| 0–1  | `01 00`    | Write prefix `0x0001`                     |
| 2–4  | `00 20 06` | Address `0x2006`                          |
| 5    | `00`       | —                                         |
| 6    | `0E`       | Payload length = **14** (6 PIN + 8 areas) |
| 7    | `35`       | Checksum                                  |


### PIN encoding (6 bytes)

Each digit → one byte; missing positions → `0xFF` (`PadRight(6)` in DLL).


| PIN      | Bytes               |
| -------- | ------------------- |
| `0411`   | `00 04 01 01 FF FF` |
| `123456` | `01 02 03 04 05 06` |


### Area block (8 bytes)

Same nibble layout as status. Set the target area's nibble to the desired mode; leave others at `0`.


| Mode | DLL constant                             | Effect  |
| ---- | ---------------------------------------- | ------- |
| `1`  | `CMD_COMANDO_AREA_INSERIMENTOTOTALE`     | Away    |
| `2`  | `CMD_COMANDO_AREA_INSERIMENTOPARZIALE`   | Stay    |
| `3`  | `CMD_COMANDO_AREA_INSERIMENTOISTANTANEO` | Instant |
| `4`  | `CMD_COMANDO_AREA_DISINSERIEMNTO`        | Disarm  |


Full frame examples (PIN `0411`, area 1):


| Operation | Payload (22 bytes hex)                         |
| --------- | ---------------------------------------------- |
| Away      | `0100002006000e3500040101ffff0100000000000000` |
| Stay      | `0100002006000e3500040101ffff0200000000000000` |
| Instant   | `0100002006000e3500040101ffff0300000000000000` |
| Disarm    | `0100002006000e3500040101ffff0400000000000000` |


Area 5, Away: `0100002006000e3500040101ffff0000010000000000` (low nibble `1` at byte 2 of area block).

Multi-area commands use the same frame with multiple non-zero nibbles (used by scenarios).

## Command response

After each 22-byte write:

```
1. Panel → 1-byte ACK
2. Client → read @ 0x2004, length 2
3. Panel → 2-byte result (e.g. 01 01 = OK)
```

Observed single-byte ACK values include `0x05`–`0x08`, `0x0F`, `0x14`, `0x24`, `0x34`, `0x44`.

## Zone status

Three separate reads (`ZoneRealTime`):


| Address  | Encoding                   | Content             |
| -------- | -------------------------- | ------------------- |
| `0x2001` | 2 bits/zone (4 zones/byte) | Terminal state      |
| `0x2002` | 1 bit/zone                 | Bypass + test flags |
| `0x2003` | 1 bit/zone                 | Alarm/tamper memory |


### Terminal state (2 bits)

```python
def zone_terminal_state(tr_zone: bytes, zone: int) -> int:  # zone 1-based
    z = zone - 1
    return (tr_zone[z // 4] >> ((z % 4) * 2)) & 0x03
```


| Value | Meaning              |
| ----- | -------------------- |
| `0`   | Rest (normal/closed) |
| `1`   | Alarm (open)         |
| `2`   | Short circuit        |
| `3`   | Line fault / tamper  |


### Bypass (1 bit from `0x2002`)

```python
def zone_excluded(zone1: bytes, zone: int) -> bool:
    z = zone - 1
    return bool((zone1[z // 8] >> (z % 8)) & 0x01)
```

Buffer lengths scale with zone count: `TRZone ≈ (n+3)//4 + 1`, `Zone1 ≈ (n+7)//8 + 1`.

Unconfigured zones often read as `0x55` patterns on a 515 panel.

## Scenarios (“Modi di Inserimento”)

Scenarios are **stored profiles** that apply target arm modes to selected areas. There is **no dedicated real-time scenario status register**.

### Configuration structure (`ModoIns`)

Each scenario (max **30**) is a record of `MAX_NUM_DATI_MODI_INS` bytes at EEPROM address `eep_prg_mod_ins`:

- `modo[]` — nibble-packed target modes (same encoding as commands). Nibble `0` = area not part of scenario.
- Additional fields: icon, output assignment, description name.


| Panel areas | Record stride | `modo[]` bytes |
| ----------- | ------------- | -------------- |
| 5           | 5             | 3              |
| 10          | 8             | 6              |
| 15          | 10            | 8              |


### Active state (binary)

A scenario is **active** when **every area included in its definition** (modes 1–4) currently matches the live area status at `0x2000`.

Example on test panel (areas 1–2 = INTERNO + BALCONE):


| Scenario name | Target modes               | Meaning                                  |
| ------------- | -------------------------- | ---------------------------------------- |
| ON            | area1=Away, area2=Away     | Both fully armed Away                    |
| PARZIALE      | area1=Stay, area2=Away     | Mixed arm modes (names are user-defined) |
| OFF           | area1=Disarm, area2=Disarm | Both disarmed                            |


> **Note:** ON / PARZIALE / OFF are **user-assigned scenario names**, not tri-state scenario statuses.

Activating a scenario = sending the scenario's `modo[]` nibbles as an area command to `0x2006`.

### Verified addresses (515 / firmware 6.09)


| Block           | Address   |
| --------------- | --------- |
| Area names      | `0x172A0` |
| Zone names      | `0x172F0` |
| Scenario names  | `0x17CF0` |
| Scenario config | `0x142BF` |


See [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) for model/firmware differences.

## SmartLeague idle polling (~2 s)

Typical cycle when connected:

```
1. Read 0x004000, 13 B  → firmware version
2. Read 0x002000, 14 B  → area status  ← primary for integrations
3. Additional internal reads (addresses vary)
```

## Repository layout

```
inim-protocol/
├── README.md              ← this document
├── docs/
│   ├── COMPATIBILITY.md   ← models, firmware, address profiles
│   └── DLL_ANALYSIS.md    ← SmartLeague DLL reverse-engineering notes
├── examples/
│   ├── inim_client.py     ← reference CLI client
│   └── alarm_proxy.py     ← transparent TCP proxy / logger
└── tools/
    └── _re_*.py           ← optional IL decompilation helpers (dnfile)
```

## Not yet mapped

- Exact `eep_prg_mod_ins` for all firmware 6.x model variants (515 verified live)
- Output commands (`0x2007`) and zone include/exclude (`0x2009`) payload formats
- Extended read prefix `10 00` high-memory tables
- AES usage paths in `smartliving.dll` on encrypted links
- Panel-wide events, faults, and multi-client behaviour

## References

- Hardware: INIM SmartLAN/SI programming port **5004**
- Software: INIM SmartLeague (`Centrale5.dll`, `smartliving.dll`)

---

*Reverse engineered June 2026 by Tiziano Zorzo [https://consulenza.app](https://consulenza.app) . Community documentation — not affiliated with INIM Electronics.*