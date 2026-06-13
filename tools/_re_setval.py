"""Trova i metodi che ASSEGNANO (stsfld) un campo statico e il valore costante precedente."""
import sys
import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase
from dncil.clr.token import Token, StringToken

DLL = r'C:\Program Files (x86)\Inim\SmartLeague\Centrale5.dll'
pe = dnfile.dnPE(DLL)
t = pe.net.mdtables
WANT = set(sys.argv[1:]) or {'MAX_NUM_PARTIZIONI', 'MAX_NUM_TERMINALI_LOGICI'}


class Reader(CilMethodBodyReaderBase):
    def __init__(self, data):
        self.data = data; self.o = 0
    def read(self, n):
        b = self.data[self.o:self.o + n]; self.o += n; return b
    def tell(self): return self.o
    def seek(self, o): self.o = o; return o


def rva_to_off(rva):
    for s in pe.sections:
        size = max(s.Misc_VirtualSize, s.SizeOfRawData)
        if s.VirtualAddress <= rva < s.VirtualAddress + size:
            return s.PointerToRawData + (rva - s.VirtualAddress)
    return None


def fld_name(tok):
    try:
        tbl = tok.value >> 24
        rid = tok.value & 0xFFFFFF
        if tbl == 0x04:
            return str(t.Field.rows[rid - 1].Name)
    except Exception:
        return None
    return None


for m in t.MethodDef.rows:
    rva = m.Rva
    if not rva:
        continue
    off = rva_to_off(rva)
    if off is None:
        continue
    data = pe.__data__[off:off + 16384]
    try:
        body = CilMethodBody(Reader(data))
    except Exception:
        continue
    ins = list(body.instructions)
    for i, x in enumerate(ins):
        if str(x.opcode) != 'stsfld':
            continue
        if not isinstance(x.operand, (Token, StringToken)):
            continue
        fn = fld_name(x.operand)
        if fn not in WANT:
            continue
        # valore costante immediatamente precedente
        prev = ins[i - 1] if i > 0 else None
        val = None
        if prev is not None and prev.operand is not None and isinstance(prev.operand, int):
            val = prev.operand
        elif prev is not None and 'ldc.i4' in str(prev.opcode):
            val = str(prev.opcode)
        print(f'{str(m.Name):40s} {fn} = {val}')
