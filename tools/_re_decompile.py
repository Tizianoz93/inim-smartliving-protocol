import sys
import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.body.reader import CilMethodBodyReaderBase
from dncil.clr.token import Token, StringToken

DLL = r'C:\Program Files (x86)\Inim\SmartLeague\Centrale5.dll'
pe = dnfile.dnPE(DLL)
t = pe.net.mdtables


class Reader(CilMethodBodyReaderBase):
    def __init__(self, data):
        self.data = data
        self.o = 0

    def read(self, n):
        b = self.data[self.o:self.o + n]
        self.o += n
        return b

    def tell(self):
        return self.o

    def seek(self, o):
        self.o = o
        return o


def rva_to_off(rva):
    for s in pe.sections:
        size = max(s.Misc_VirtualSize, s.SizeOfRawData)
        if s.VirtualAddress <= rva < s.VirtualAddress + size:
            return s.PointerToRawData + (rva - s.VirtualAddress)
    return None


def get_body(rva):
    off = rva_to_off(rva)
    data = pe.__data__[off:off + 16384]
    return CilMethodBody(Reader(data))


def resolve(tok):
    try:
        if isinstance(tok, StringToken):
            us = pe.net.user_strings.get(tok.rid)
            return 'str:' + repr(getattr(us, 'value', us))
    except Exception:
        pass
    try:
        tbl = tok.table if hasattr(tok, 'table') else (tok.value >> 24)
        rid = tok.rid if hasattr(tok, 'rid') else (tok.value & 0xFFFFFF)
        if tbl == 0x0A:
            return 'mref:' + str(t.MemberRef.rows[rid - 1].Name)
        if tbl == 0x06:
            return 'meth:' + str(t.MethodDef.rows[rid - 1].Name)
        if tbl == 0x04:
            return 'fld:' + str(t.Field.rows[rid - 1].Name)
        return 'tok:%08x' % tok.value
    except Exception:
        try:
            return 'tok:%08x' % tok.value
        except Exception:
            return 'tok:?'


def dump(name):
    for m in t.MethodDef.rows:
        if str(m.Name) != name:
            continue
        rva = m.Rva
        if not rva:
            print('==== %s (no body) ====' % name)
            return
        print('==== %s (rva=0x%x) ====' % (name, rva))
        try:
            body = get_body(rva)
            for ins in body.instructions:
                op = str(ins.opcode)
                operand = ''
                if ins.operand is not None:
                    if isinstance(ins.operand, (Token, StringToken)):
                        operand = resolve(ins.operand)
                    else:
                        operand = str(ins.operand)
                print('  %04x %-15s %s' % (ins.offset, op, operand))
        except Exception as e:
            print('  ERR', repr(e))
        print()


if __name__ == '__main__':
    for n in sys.argv[1:]:
        dump(n)
