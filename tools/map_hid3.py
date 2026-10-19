#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Dump the function that resolves HID pointers (RVA 0x50c50) and its call sites."""
import sys
sys.path.insert(0, '/tmp/opencode/py')
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

pe = pefile.PE('temp/extracted/app/DeviceDriver.exe')
image = pe.get_memory_mapped_image()
md = Cs(CS_ARCH_X86, CS_MODE_32)
md.skipdata = True

def disasm(rva, size=0x180, before=0):
    code = image[rva - before: rva - before + size]
    out = []
    for insn in md.disasm(code, rva - before):
        mark = ' <<<' if insn.address == rva else ''
        out.append(f'{insn.address:08x}: {insn.mnemonic:8s} {insn.op_str}{mark}')
    return '\n'.join(out)

print('===== RESOLVER FUNCTION (RVA 0x50c50) =====')
print(disasm(0x50c50, 0x190))
print('\n===== CALL SITE 0x51015 (ptr 0x5950b4) =====')
print(disasm(0x51015, 0x60, 0x50))
print('\n===== CALL SITE 0x5111c (ptr 0x5950b8) =====')
print(disasm(0x5111c, 0x60, 0x50))
print('\n===== CALL SITE 0x5163a (ptr 0x5950a0) =====')
print(disasm(0x5163a, 0x60, 0x50))
