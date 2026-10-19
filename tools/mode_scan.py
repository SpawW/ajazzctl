#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Light mode scan: send modes 0..15 with a fixed RED color, 3 s each.

Usage: sudo python3 tools/mode_scan.py [color=FF0000]
Watch the keyboard and note which mode(s) keep the color SOLID (no animation).
"""
import sys, time, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ajazz_ctl import (AjazzKeyboard, find_candidates, _probe_one, _color,
                       PROTO_MARK, CMD_OPEN)

def main():
    color = _color(sys.argv[1]) if len(sys.argv) > 1 else (0xFF, 0x00, 0x00)
    cands = find_candidates()
    kb = None
    for path, vid, pid, iface in cands:
        if _probe_one(path):
            kb = AjazzKeyboard(path)
            print(f"using {path} (if={iface})")
            break
    if not kb:
        sys.exit("no interface answered")
    try:
        for m in range(16):
            ok = kb.set_light(mode=m, color=color, brightness=5, speed=3)
            print(f"mode {m:2d} -> {'ACK' if ok else 'no ACK'}   "
                  f"(color #{''.join(f'{c:02X}' for c in color)}, 3 s)",
                  flush=True)
            time.sleep(3)
    finally:
        kb.close()

if __name__ == "__main__":
    main()
