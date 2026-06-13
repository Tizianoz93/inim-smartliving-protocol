# Optional reverse-engineering helpers

Scripts for IL disassembly of SmartLeague `Centrale5.dll` using [dnfile](https://github.com/distribution/dnfile) and [dncil](https://github.com/distribution/dncil).

```bash
pip install dnfile dncil
python _re_decompile.py EseguiComando
python _re_decompile.py AreeRealTime
python _re_find.py 8192
```

Edit the DLL path inside each script to match your SmartLeague installation.

These tools are **not** required to use the reference client or proxy.
