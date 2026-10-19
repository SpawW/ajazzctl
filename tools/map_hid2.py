#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find all references to the HID pointer block (0x595080-0x595100) and disassemble them with skipdata."""
import sys
sys.path.insert(0, '/tmp/opencode/py')
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

BIN = 'temp/extracted/app/DeviceDriver.exe'
pe = pefile.PE(BIN)
image = pe.get_memory_mapped_image()
base = pe.OPTIONAL_HEADER.ImageBase
md = Cs(CS_ARCH_X86, CS_MODE_32)
md.skipdata = True

sec = {s.Name.rstrip(b'\0'): (s.VirtualAddress, s.Misc_VirtualSize) for s in pe.sections}
text_va, text_sz = sec[b'.text']
tb = image[text_va:text_va + text_sz]

def disasm(va, size=0x80, before=0x20):
    start = max(text_va, va - before)
    code = image[start - base: start - base + size + before]
    out = []
    for insn in md.disasm(code, start):
        mark = ' <<<' if insn.address == va else ''
        out.append(f'  {insn.address:08x}: {insn.mnemonic:8s} {insn.op_str}{mark}')
    return '\n'.join(out)

# find possible prologues push ebp; mov ebp,esp before 0x50d60
print('== possible prologues of the resolver function ==')
for i in range(0x50800 - text_va, 0x50d60 - text_va):
    if tb[i:i+4] == b'\x55\x8b\xec\x83':
        print(f'  {text_va + i:#x}')

# all 4-byte references to the block
print('\n== references to the pointer block ==')
sites = {}
i = 0
while True:
    i += 1
    if i + 4 > len(tb):
        break
    val = int.from_bytes(tb[i:i+4], 'little')
    if 0x595080 <= val <= 0x595100:
        sites.setdefault(val, []).append(text_va + i)
for t in sorted(sites):
    print(f'\n  [{t:#x}] refs at: {[hex(a) for a in sites[t]]}')
    for a in sites[t]:
        print(disasm(a, 0x40, 0x30))
