#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Test ONE light-packet field at a time (confirm src[8..11] order).

Usage: sudo python3 tools/set_field.py <field> <value> [mode=7] [color=FF0000]
  field in {bri, vel, dir, colorful}
E.g.: sudo python3 tools/set_field.py bri 0 7   # brightness 0 in breathing mode
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ajazz_ctl import AjazzKeyboard, find_candidates, _probe_one, _color

FIELDS = {"bri": "brightness", "vel": "speed", "dir": "direction",
          "colorful": "colorful"}

def main():
    if len(sys.argv) < 3 or sys.argv[1] not in FIELDS:
        sys.exit(__doc__)
    # first positional: target field; extras use field=value (e.g. bri=0)
    field = FIELDS[sys.argv[1]]
    value = int(sys.argv[2], 0)

    kwargs = dict(mode=7, color=(0xFF, 0x00, 0x00), brightness=5, speed=3,
                  direction=0, colorful=0)
    # positional args: mode and color; extras use field=value
    rest = sys.argv[3:]
    pos = []
    for a in rest:
        if "=" in a:
            k, v = a.split("=", 1)
            if k not in FIELDS:
                sys.exit(f"unknown field: {k}")
            kwargs[FIELDS[k]] = int(v, 0)
        else:
            pos.append(a)
    if pos:
        kwargs["mode"] = int(pos[0])
    if len(pos) > 1:
        kwargs["color"] = _color(pos[1])
    kwargs[field] = value

    for path, vid, pid, iface in find_candidates():
        if _probe_one(path):
            with AjazzKeyboard(path) as kb:
                ok = kb.set_light(**kwargs)
            print(f"{sys.argv[1]}={value} (mode {kwargs['mode']} "
                  f"bri={kwargs['brightness']} vel={kwargs['speed']} "
                  f"dir={kwargs['direction']} colorful={kwargs['colorful']}) "
                  f"-> {'ACK' if ok else 'no ACK'} ({path})")
            return 0
    sys.exit("no interface answered")

if __name__ == "__main__":
    main()
