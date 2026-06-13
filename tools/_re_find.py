import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase

DLL = r'C:\Program Files (x86)\Inim\SmartLeague\Centrale5.dll'
pe = dnfile.dnPE(DLL)
t = pe.net.mdtables


class Reader(CilMethodBodyReaderBase):
    def __init__(self, data):
        self.data = data
        self.o = 0
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


TARGETS = {0x2000, 0x2001, 0x2002, 0x2003}
found = {}
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
    vals = set()
    for ins in body.instructions:
        if ins.operand is not None and isinstance(ins.operand, int):
            if ins.operand in TARGETS:
                vals.add(ins.operand)
    if vals:
        found.setdefault(str(m.Name), set()).update(vals)

for name, vals in sorted(found.items()):
    print(name, ['0x%x' % v for v in sorted(vals)])
print('total', len(found))
