import sys
import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase
from dncil.clr.token import Token, StringToken

DLL = r'C:\Program Files (x86)\Inim\SmartLeague\Centrale5.dll'
pe = dnfile.dnPE(DLL)
t = pe.net.mdtables
WANT = sys.argv[1] if len(sys.argv) > 1 else 'com_read_stato_area'


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
        if tbl == 0x0A:
            return str(t.MemberRef.rows[rid - 1].Name)
    except Exception:
        return None
    return None


hits = []
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
    for ins in body.instructions:
        if ins.operand is not None and isinstance(ins.operand, (Token, StringToken)):
            n = fld_name(ins.operand)
            if n == WANT:
                hits.append(str(m.Name))
                break

for h in sorted(set(hits)):
    print(h)
print('total', len(set(hits)))
