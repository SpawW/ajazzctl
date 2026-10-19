#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/sysread.py - experiment: read the keyboard system settings.

Sends command `04 20` (with src[8]=3 or 8) and dumps the feature-report
responses, to discover where the fields live (fn_switch, key_respondtime,
sleep_time, ...).

Usage:  sudo python3 tools/sysread.py [3|8]
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ajazz_ctl as A  # noqa: E402


def main():
    sel = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    cand = A.find_candidates()
    dev = None
    for path, vid, pid, iface in cand:
        kb = A.AjazzKeyboard(path)
        if kb.cmd([A.PROTO_MARK, A.CMD_OPEN], retries=1):
            dev = (kb, path, vid, pid, iface)
            break
        kb.close()
    if dev is None:
        print("no interface answered the 04 18 handshake")
        return 1
    kb, path, vid, pid, iface = dev
    print(f"using {path} (VID {vid:04x} PID {pid:04x} iface {iface})")

    # read command: 04 20 with src[8]=sel
    req = bytearray(0x40)
    req[0] = A.PROTO_MARK
    req[1] = 0x20
    req[8] = sel
    wire = kb._wire(bytes(req))
    kb.set_feature(wire)
    time.sleep(0.05)

    print("--- get_feature responses ---")
    for n in range(12):
        try:
            resp = kb.get_feature(wire)
        except OSError as e:
            print(f"[{n:2}] OSError: {e}")
            break
        print(f"[{n:2}] {resp.hex(' ')}")
        time.sleep(0.03)
    kb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
