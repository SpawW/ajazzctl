#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate the code that loads hid.dll and calls HidD_SetFeature/GetFeature."""
import sys
sys.path.insert(0, '/tmp/opencode/py')
import pefile
from capstone import Cs, CS_ARCH_X86, CS_MODE_32

BIN = 'temp/extracted/app/DeviceDriver.exe'
pe = pefile.PE(BIN)
image = pe.get_memory_mapped_image()
text = None
for s in pe.sections:
    if s.Name.rstrip(b'\0') == b'.text':
        text = (s.VirtualAddress, s.Misc_VirtualSize)
text_va, text_size = text
text_bytes = image[text_va:text_va + text_size]

md = Cs(CS_ARCH_X86, CS_MODE_32)
md.detail = False

# 1. find the strings and their VAs
needles = [b'hid.dll\x00', b'HidD_SetFeature\x00', b'HidD_GetFeature\x00',
           b'HidD_GetPreparsedData\x00', b'HidD_FreePreparsedData\x00',
           b'HidP_GetCaps\x00', b'HidD_GetAttributes\x00', b'HIDClass\x00',
           b'HidD_GetProductString\x00']
va_of = {}
for n in needles:
    off = image.find(n)
    if off != -1:
        # VA = RVA + imagebase; convert the mapped-image offset to a VA
        va = pe.OPTIONAL_HEADER.ImageBase + off
        va_of[n.rstrip(b'\x00').decode()] = va
        print(f'string {n.rstrip(chr(0).encode()).decode():26s} VA={va:#010x}')

# 2. find immediate references in .text (push/lea/mov imm32)
def find_refs(va):
    pat = va.to_bytes(4, 'little')
    refs = []
    start = 0
    while True:
        i = text_bytes.find(pat, start)
        if i < 0:
            break
        refs.append(text_va + i)
        start = i + 1
    return refs

for name, va in sorted(va_of.items()):
    refs = find_refs(va)
    print(f'\n== refs to {name}: {[hex(r) for r in refs]}')
    for r in refs:
        # disassemble around the reference
        start = max(text_va, r - 0x40)
        code = image[start:r + 0x40]
        print(f'--- around {r:#x} ---')
        for insn in md.disasm(code, start):
            mark = ' <<<' if insn.address == r else ''
            print(f'  {insn.address:08x}: {insn.mnemonic:8s} {insn.op_str}{mark}')
