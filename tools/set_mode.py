#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Set a single light mode (one-by-one test).

Usage: sudo python3 tools/set_mode.py <mode> [color=FF0000] [brightness=5] [speed=3]
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ajazz_ctl import AjazzKeyboard, find_candidates, _probe_one, _color

def main():
    mode = int(sys.argv[1])
    color = _color(sys.argv[2]) if len(sys.argv) > 2 else (0xFF, 0x00, 0x00)
    bri = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    speed = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    for path, vid, pid, iface in find_candidates():
        if _probe_one(path):
            with AjazzKeyboard(path) as kb:
                ok = kb.set_light(mode=mode, color=color, brightness=bri,
                                  speed=speed)
            print(f"mode {mode} color=#{''.join(f'{c:02X}' for c in color)} "
                  f"brightness={bri} speed={speed} -> "
                  f"{'ACK' if ok else 'no ACK'} ({path})")
            return 0
    sys.exit("no interface answered")

if __name__ == "__main__":
    main()
