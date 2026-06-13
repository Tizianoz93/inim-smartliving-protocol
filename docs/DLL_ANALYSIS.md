# SmartLeague DLL analysis

Static reverse engineering of the Windows SmartLeague install (`C:\Program Files (x86)\Inim\SmartLeague\`). Managed assemblies were disassembled with **dnSpyEx** and batch IL dumps via `dnfile` / `dncil` (`tools/_re_decompile.py`). Native DLLs were skimmed in Ghidra for exports and string tables.

Frames rebuilt from decompiled logic match TCP captures from `examples/alarm_proxy.py` byte-for-byte. That cross-check is the main validation path for everything below.

## Software stack

SmartLeague is a .NET host talking to a native TCP library. Protocol framing and memory addresses are **not** embedded as static blobs in the transport layer — they are assembled in managed code before `net_sl_write`.

```
Smartleague.exe          Firebird DB, session, DllImport
    │
    ├── Centrale5.dll    SmartLiving logic — CMD_* constants, frame build, UI
    │       └── P/Invoke → smartliving.dll, transfer.dll
    │
    └── smartliving.dll    native TCP :5004, net_sl_read / net_sl_write
            └── socklib.dll
```

| Binary | Role in protocol RE |
|--------|---------------------|
| **`Centrale5.dll`** | Primary source — area/zone/scenario decode, command encode, RAM map init |
| **`smartliving.dll`** | Transport only — socket I/O, optional AES; handshake string `pass` |
| **`Smartleague.exe`** | Host — opens session, passes buffers to native write |
| **`Centrale1.dll`** | Different product line UI plugin; ignore for SmartLiving |
| **`myLib.dll`** | Model strings (`INIM_SMARTLIVING_515`, …), DB helpers, TCP password keys |

For a SmartLiving panel, start in **`Centrale5.dll`**, not `Centrale1.dll`.

## `smartliving.dll` (native)

MinGW-built C library (`libsmartliving.c`, `net.c`, `aes.c` — build paths appear in strings). Relevant exports:

| Export | Purpose |
|--------|---------|
| `net_sl_open` / `net_sl_close` | Session lifecycle |
| `net_sl_read` / `net_sl_write` | Raw socket I/O used by managed code |
| `sl_aes_*`, `sl_enc` / `sl_dec` | AES helpers — LAN traffic observed in cleartext, but crypto exists |

The 8-byte read header and 22-byte area command (8-byte write header + 14-byte payload) never show up as fixed byte patterns inside this DLL. Search here only if you need transport details or encryption behaviour.

## `Centrale5.dll` — decoded protocol surface

### RAM map (`InizializzaRam_*`)

Every firmware variant (`InizializzaRam_1_00` … `_6_00`) assigns the **same** realtime addresses. Model-specific code only changes counts (areas, zones, scenario stride). See [COMPATIBILITY.md](COMPATIBILITY.md) for the full parameter table.

| Address | Field / constant | Access |
|---------|------------------|--------|
| `0x2000` | `com_read_stato_area` | read |
| `0x2001` | `com_read_tr_zone` | read |
| `0x2002` | `com_read_stato_zone1` | read |
| `0x2003` | `com_read_stato_zone2` | read |
| `0x2004` | `CMD_READ_ESITO_COM_PC` | read |
| `0x2006` | `CMD_AREA_INSERIMENTI_ADDRESS` | write |
| `0x2007` | `CMD_USCITA_ATTDIS_ADDRESS` | write |
| `0x2008` | `CMD_AREA_RESETAREA_ADDRESS` | write |
| `0x2009` | `CMD_ZONA_INCESC_ADDRESS` | write |
| `0x4000` | firmware version string | read |
| `0x4014` / `0x4016` / `0x4024` | pointers to name blocks (fw ≤ 5.x) | read |

Name blocks are `N × 16` bytes of space-padded ASCII. Firmware 6.x often returns bad pointers at `0x4014` — direct EEPROM addresses are model/firmware specific (documented under field-tested notes in COMPATIBILITY).

### Area mode constants

Extracted from metadata / `btnInserimento*_Click` handlers:

| Constant | Value | Meaning |
|----------|-------|---------|
| `CMD_COMANDO_AREA_INSERIMENTOTOTALE` | `1` | Away (total) |
| `CMD_COMANDO_AREA_INSERIMENTOPARZIALE` | `2` | Stay (partial) |
| `CMD_COMANDO_AREA_INSERIMENTOISTANTANEO` | `3` | Instant |
| `CMD_COMANDO_AREA_DISINSERIEMNTO` *(sic)* | `4` | Disarm |
| `CMD_COMANDO_AREA_RESET` | `5` | Area reset |

The disarm constant name is misspelled in the binary (`DISINSERIEMNTO`).

### `EseguiComando` — arm/disarm frame

Area commands are a **write** to `0x2006`: 6-byte PIN + 8-byte area block (14-byte payload, 22 bytes on the wire including the write header).

```text
pin6 = PadRight(pin, 6)     # each digit → one byte; unused positions → 0xFF
block = byte[8]             # zero-initialized
p = area - 1                # 0-based partition index
idx = p // 2
if p even:  block[idx] |= mode & 0x0F
else:       block[idx] |= (mode << 4) & 0xF0
write_d_d(pin6 + block, len=14, addr=0x2006)
```

Only the target area nibble is set; other nibbles stay zero. UI buttons pass `mode` 1–4 as in the table above.

### `AreeRealTime` — area status buffer

```text
len = MAX_PARTITIONS/2 + 1 + 10
buf = read_d_d(addr=0x2000, len)
for p in 0 .. MAX_PARTITIONS-1:
    mode = low_nibble(buf[p/2])  if p even else high_nibble(buf[p/2])
    alarm        = bit(buf[MAX/2 + 1 + p/8], p % 8)
    alarm_memory = bit(buf[MAX/2 + 3 + p/8], p % 8)
```

Status reads and command writes share the **same nibble layout** (two areas per byte). An early wire hypothesis — one status byte per area — was wrong; the DLL indexing makes that clear.

For partition index `p`, the mode nibble lives in byte `p/2` (low nibble when `p` is even, high when odd).

### `ZoneRealTime` — zone inputs

Three separate reads:

```text
TRZone = read(0x2001)   # terminal state, 2 bits per zone
Zone1  = read(0x2002)   # bypass / test flags, 1 bit per zone
Zone2  = read(0x2003)   # alarm / tamper memory
```

Terminal decode (`DeterminaStatoTerminale`):

```text
state = (TRZone[z/4] >> (z % 4) * 2) & 3
  0 = rest    1 = alarm
  2 = short   3 = fault
```

### Scenarios (`ModoIns` / `LeggiModiInserimento20`)

No realtime scenario register exists in the RAM map. Scenario **activity** is derived by comparing each slot's `modo[]` targets against live area modes at `0x2000`.

Configuration is read from EEPROM address `eep_prg_mod_ins` (value depends on firmware — see COMPATIBILITY). Each scenario occupies `MAX_NUM_DATI_MODI_INS` bytes; `modo[]` inside a slot is nibble-packed the same way as area commands. Helper `RestituisciNibble(sel, byte)` returns the low nibble when `sel == 0`, high nibble otherwise.

## Correlating DLL logic with captures

| Capture observation | DLL source |
|---------------------|------------|
| Handshake `pass` after TCP connect | string in `smartliving.dll` |
| 8-byte poll frames | built in managed code, sent via `net_sl_write` |
| 22-byte arm/disarm writes | `EseguiComando` payload layout |
| PIN bytes ending in `FF FF` | `PadRight(6)` — not a 4-digit PIN plus separator |
| Two areas sharing one status byte | `AreeRealTime` nibble extract |
| Command result polling | read `0x2004` (`CMD_READ_ESITO_COM_PC`) |

When in doubt, set `alarm_proxy.py` between SmartLeague and the panel, trigger an action in the UI, and disassemble the matching method in `Centrale5.dll`.

## Suggested RE workflow

1. **dnSpyEx** — open `Centrale5.dll`, search `CMD_COMANDO_AREA_INSERIMENTOTOTALE`, follow uses of `CMD_AREA_INSERIMENTI_ADDRESS`.
2. Dump IL for hot methods:
   ```bash
   pip install dnfile dncil
   python tools/_re_decompile.py EseguiComando
   python tools/_re_decompile.py AreeRealTime
   python tools/_re_decompile.py ZoneRealTime
   python tools/_re_decompile.py InizializzaRam_5_00
   ```
   Edit the DLL path at the top of `_re_decompile.py` to match your SmartLeague install.
3. **Ghidra** — `smartliving.dll` / `socklib.dll` only if you need native transport or AES details.
4. **Validate** — reproduce the frame in `examples/inim_client.py` and compare to a proxy log.

## Related docs

- [README.md](../README.md) — wire format, checksum, chunked reads
- [COMPATIBILITY.md](COMPATIBILITY.md) — model counts, firmware 6.x name addresses, scenario EEPROM
