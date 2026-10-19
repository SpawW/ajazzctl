#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Map HID pointers resolved via GetProcAddress and find all call sites."""
import sys
sys.path.insert(0, '/tmp/opencode/py')
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

BIN = 'temp/extracted/app/DeviceDriver.exe'
pe = pefile.PE(BIN)
image = pe.get_memory_mapped_image()
base = pe.OPTIONAL_HEADER.ImageBase
md = Cs(CS_ARCH_X86, CS_MODE_32)

sec = {s.Name.rstrip(b'\0'): (s.VirtualAddress, s.Misc_VirtualSize) for s in pe.sections}

def va2off(va):
    return va - base

def disasm(va, size=0x100, before=0):
    start = va - before
    code = image[va2off(start):va2off(start + size)]
    out = []
    for insn in md.disasm(code, start):
        mark = ' <<<' if insn.address == va else ''
        out.append(f'  {insn.address:08x}: {insn.mnemonic:8s} {insn.op_str}{mark}')
    return '\n'.join(out)

# strings around the resolutions
print('== strings in the hid block ==')
for va in range(0x56fe90, 0x56ffd0, 1):
    chunk = image[va2off(va):va2off(va) + 40]
    if chunk[0:1].isalpha() and b'\x00' in chunk:
        s = chunk.split(b'\x00')[0]
        if s.isascii() and len(s) > 3:
            print(f'  {va:#x}: {s.decode()}')

# loader function: disassemble from 0x50c00 to 0x50de0
print('\n== resolver function (0x50c00-0x50de0) ==')
print(disasm(0x50c00, 0x1e0))

# find all 'call dword ptr [X]' where X is in 0x595000-0x595200
text_va, text_sz = sec[b'.text']
tb = image[text_va:text_va + text_sz]
print('\n== pointer call sites ==')
import re
i = 0
sites = {}
while True:
    i = tb.find(b'\xff\x15', i)  # call dword ptr [imm32]
    if i < 0:
        break
    addr = text_va + i
    target = int.from_bytes(tb[i+2:i+6], 'little')
    if 0x595000 <= target <= 0x595400:
        sites.setdefault(target, []).append(addr)
    i += 2
for t in sorted(sites):
    print(f'  ptr [{t:#x}] called at: {[hex(a) for a in sites[t]]}')
