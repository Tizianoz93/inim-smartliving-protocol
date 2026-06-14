# Tools

## `dump_memory.py`

Reads a range of panel memory over TCP and saves it to a binary file (256 bytes per request, per protocol limit).

```bash
export INIM_HOST=192.168.1.50
python dump_memory.py --start 0 --end 0x20000 --out panel.bin
```

Also writes `panel.bin.strings.txt` — 16-byte printable slots with addresses (useful for locating name tables).

Address reference: [docs/MEMORY_MAP.md](../docs/MEMORY_MAP.md), [docs/COMPATIBILITY.md](../docs/COMPATIBILITY.md).

Not required for the reference client in `examples/`.
