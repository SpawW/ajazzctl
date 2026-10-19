#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AJAZZ Linux contributors
"""
ajazz_ctl.py - Linux controller for AJAZZ tri-mode RGB keyboards.

Reimplements, over hidraw, the protocol reverse-engineered from the Windows
vendor driver (see PROTOCOL.md).

Standard library only (fcntl.ioctl + sysfs). Usage:

    sudo ./ajazz_ctl.py list                  # list candidate hidraw interfaces
    sudo ./ajazz_ctl.py probe                 # test the handshake (04 18 + ACK)
    sudo ./ajazz_ctl.py light --mode 1 --color FF8800 --brightness 5 --speed 3
    sudo ./ajazz_ctl.py raw 04 18 --ack       # send a raw payload (after report ID)
    sudo ./ajazz_ctl.py raw --wire 00 04 18   # send a full 65-byte report

A udev rule (99-ajazz-ak820-pro.rules) avoids the need for sudo.
"""

import argparse
import fcntl
import os
import sys
import time

# ---------------------------------------------------------------- constants

VID_SONIX = 0x0C45
PID_DEVICES = {
    0x8006: "AJAZZ keyboard (USB)",
    0x8009: "AJAZZ AK820 Pro (USB)",
    0xFEFE: "2.4G dongle",
}

REPORT_LEN = 65          # FeatureReportByteLength (0x41), same as the driver
PROTO_MARK = 0x04        # byte 1 of every control command
ACK_OK = 0x01            # response[4] == 1 -> keyboard acknowledged

# decoded commands
CMD_SAVE = 0x02
CMD_SECTION = 0x13
CMD_OPEN = 0x18
CMD_APPLY = 0xF0
CMD_SCREEN_CLOCK = 0x28   # screen: clock sync (src[8]=1)
CMD_SCREEN_GIF = 0x72     # screen: image/GIF upload (4 KB blocks)
CMD_SYSSET = 0x17         # system settings (sleep time, fn, key response)

# "Sleep Time" (idle backlight timeout) - values from the vendor app combo
# (language/1033.lan: 142=No Sleep, 143=1min, 144=5min, 145=30min)
SLEEP_MODES = {
    0: "No sleep (always on)",
    1: "1 minute",
    2: "5 minutes",
    3: "30 minutes",
}

# Light mode IDs confirmed on hardware (tested one by one, real descriptions):
#   0 = off                            8 = color cycle (whole board, hue scale)
#   1 = static rainbow                 9 = ripple (stone in the lake, from center)
#   2 = REACTIVE fixed per-key color  10 = rainbow wave (horizontal bands)
#       (off; on keypress that key lights up ~2 s with a random
#        fixed per-key color - blue/green, orange at the edges)
#   3 = random FIXED colors per key (static christmas tree)
#   4 = slow twinkle (random keys blink randomly)
#   5 = starlight (each LED blinks in a different color, no rotation)
#   6 = spectrum (all on, each LED color changes across a scale)
#   7 = breathing (slow pulse in the selected color)
#  11 = diagonal wave 45 deg (left->right)   14 = reactive ripple (wave from key)
#  12 = rotating wave (spins)                15 = reactive wave (2x wider than 14)
LIGHT_MODES = {
    0: "Off",
    1: "Static rainbow",
    2: "Reactive (fixed per-key color on keypress)",
    3: "Random fixed colors per key",
    4: "Twinkle (random keys blink slowly)",
    5: "Starlight (blinks, different colors)",
    6: "Spectrum (each LED color changes across a scale)",
    7: "Breathing (pulses in the selected color)",
    8: "Color cycle (whole board changes color)",
    9: "Ripple (stone in the lake, from center)",
    10: "Rainbow wave (horizontal bands)",
    11: "Diagonal wave (45 degrees, left->right)",
    12: "Rotating wave (spins like a clock)",
    13: "Reactive line (color/intensity on the key's row)",
    14: "Reactive ripple (wave from the pressed key)",
    15: "Reactive wave (2x wider than 14)",
}


def _ioc(direction, type_, nr, size):
    return (direction << 30) | (size << 16) | (ord(type_) << 8) | nr


_IOC_READ, _IOC_WRITE = 1, 2


def HIDIOCSFEATURE(length):   # feature report write ioctl
    return _ioc(_IOC_READ | _IOC_WRITE, "H", 0x06, length)


def HIDIOCGFEATURE(length):   # feature report read ioctl
    return _ioc(_IOC_READ | _IOC_WRITE, "H", 0x07, length)


# ------------------------------------------------------------- discovery


def iter_hidraw():
    """Yield (path, vid, pid, iface) for every /dev/hidraw* in sysfs."""
    base = "/sys/class/hidraw"
    if not os.path.isdir(base):
        return
    for name in sorted(os.listdir(base)):
        dev = os.path.join(base, name, "device")
        # generic, bus-agnostic: HID_ID=bus:vid:pid in the HID device uevent
        vid = pid = None
        try:
            for line in open(os.path.join(dev, "uevent")):
                if line.startswith("HID_ID="):
                    _, vid_s, pid_s = line.split()[0].split("=")[1].split(":")
                    vid, pid = int(vid_s, 16), int(pid_s, 16)
                    break
        except (OSError, ValueError):
            continue
        if vid is None:
            continue
        iface = "?"
        try:
            iface = open(os.path.join(os.path.realpath(dev), "..",
                                      "bInterfaceNumber")).read().strip()
        except OSError:
            pass
        yield f"/dev/{name}", vid, pid, iface


def find_candidates(only_vendor=True):
    out = []
    for path, vid, pid, iface in iter_hidraw():
        if only_vendor and vid != VID_SONIX:
            continue
        out.append((path, vid, pid, iface))
    return out


# ------------------------------------------------------------ device


class AjazzKeyboard:
    """HID session with the keyboard (65-byte feature reports)."""

    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---- low level ----

    def _wire(self, payload):
        """src (<=64 bytes) -> 65-byte report with report ID 0."""
        if len(payload) > REPORT_LEN - 1:
            raise ValueError("payload > 64 bytes")
        return bytes([0]) + bytes(payload).ljust(REPORT_LEN - 1, b"\x00")

    def set_feature(self, wire):
        buf = bytearray(wire[:REPORT_LEN].ljust(REPORT_LEN, b"\x00"))
        fcntl.ioctl(self.fd, HIDIOCSFEATURE(len(buf)), buf)

    def get_feature(self, wire):
        buf = bytearray(wire[:REPORT_LEN].ljust(REPORT_LEN, b"\x00"))
        fcntl.ioctl(self.fd, HIDIOCGFEATURE(len(buf)), buf)
        return bytes(buf)

    # ---- protocol layer ----

    def cmd(self, payload, wait_ack=True, retries=3):
        """Send a command (payload without the report ID byte). True on ACK."""
        wire = self._wire(payload)
        for _ in range(retries):
            try:
                self.set_feature(wire)
            except OSError:
                time.sleep(0.1)
                continue
            if not wait_ack:
                return True
            time.sleep(0.035)   # cmd_delaytime=35 from config.xml
            try:
                resp = self.get_feature(wire)
            except OSError:
                # timeout/EPIPE: device busy or report not supported
                time.sleep(0.1)
                continue
            if resp[4] == ACK_OK:
                return True
            time.sleep(0.05)
        return False

    def open_session(self):
        """Apply steps 1+2: open the session and select the write section."""
        if not self.cmd([PROTO_MARK, CMD_OPEN], wait_ack=True):
            return False
        section = [PROTO_MARK, CMD_SECTION] + [0] * 6
        section.append(1)                      # src[8] = 1 -> wire[9] = 1
        return self.cmd(section, wait_ack=True)

    def close_session(self, apply=True):
        """Steps 4+5: save (ACK) and apply/exit (no ACK)."""
        ok = self.cmd([PROTO_MARK, CMD_SAVE], wait_ack=True)
        self.cmd([PROTO_MARK, CMD_APPLY], wait_ack=False)
        if apply:
            time.sleep(0.02)
        return ok

    def write_data(self, src):
        """Send a data packet (terminated with AA 55 by the caller if needed)."""
        return self.cmd(src, wait_ack=False)

    # ---- high level ----

    def set_light(self, mode=1, color=(0xFF, 0x88, 0x00), brightness=5, speed=3,
                  direction=0, colorful=0):
        """Write the lighting configuration (driver constructor 0x2b000).

        Field mapping CONFIRMED on hardware (see PROTOCOL.md section 6):
          src[0]=mode, src[1..3]=R,G,B, src[8]=colorful (0=single color, >0=rainbow),
          src[9]=brightness (0..5), src[10]=speed (0..5), src[11]=direction,
          AA55 magic at src[0x0e].
        """
        src = bytearray(0x40)
        src[0] = mode & 0xFF
        src[1], src[2], src[3] = (c & 0xFF for c in color)
        src[8] = colorful & 0xFF
        src[9] = brightness & 0xFF
        src[10] = speed & 0xFF
        src[11] = direction & 0xFF
        src[0x0E] = 0xAA
        src[0x0F] = 0x55
        if not self.open_session():
            return False
        ok = self.write_data(src)
        return self.close_session() and ok

    # ---- TFT screen ----

    def sync_clock(self, index=0, when=None):
        """Set the TFT screen date/time (cmd 04 28, src[8]=1).

        Sequence (decoded from driver function RVA 0x23a80):
          04 18 -> 04 28 (src[8]=1) -> clock packet -> 04 02

        Clock packet (the device sees the driver's whole buffer):
          pkt[0]=0x00, pkt[1]=screen item index + 1, pkt[2]=0x5A (marker),
          pkt[3]=year % 2000, pkt[4]=month, pkt[5]=day,
          pkt[6]=hour, pkt[7]=minute, pkt[8]=second,
          pkt[10]=day of week (0=Sunday), AA55 at pkt[0x3e..0x3f].
        """
        t = time.localtime(when)
        if not self.cmd([PROTO_MARK, CMD_OPEN]):
            return False

        head = bytearray(0x40)
        head[0] = PROTO_MARK
        head[1] = CMD_SCREEN_CLOCK
        head[8] = 1                        # src[8] = 1 -> clock sync
        if not self.cmd(bytes(head)):
            return False

        # NOTE on the offset: in the driver the buffer ([ebp-0x50]) has buf[0]=0x00
        # (same convention as the light packet, where buf[0]=mode). The device
        # sees buf[0..63], i.e. exactly this payload. Hence the leading 0x00.
        pkt = bytearray(0x40)
        pkt[0] = 0x00                      # data packet convention
        pkt[1] = (index + 1) & 0xFF        # selected screen item
        pkt[2] = 0x5A                      # clock marker
        pkt[3] = (t.tm_year % 2000) & 0xFF
        pkt[4] = t.tm_mon & 0xFF
        pkt[5] = t.tm_mday & 0xFF
        pkt[6] = t.tm_hour & 0xFF
        pkt[7] = t.tm_min & 0xFF
        pkt[8] = t.tm_sec & 0xFF
        pkt[10] = (t.tm_wday + 1) % 7      # Windows: 0=Sunday; Python: 0=Monday
        pkt[0x3E] = 0xAA
        pkt[0x3F] = 0x55
        self.write_data(pkt)
        return self.cmd([PROTO_MARK, CMD_SAVE])

    # ---- system settings ----

    def set_sleep(self, value, fn_switch=0, key_respond=0):
        """Set the "Sleep Time" (idle backlight/screen timeout).

        Decoded from driver function RVA 0x141a0 (USB variant; the 2.4G mode
        uses a different header). Sequence: 04 18 -> 04 17 (src[2]=1, src[8]=1)
        -> data packet -> 04 02. No 04 13/04 F0.

        Data packet (buffer seen by the keyboard):
          buf[0]=0x00, buf[1]=0x01,
          buf[2..4] = disable Windows key / ALT+F4 / ALT+TAB (0/1),
          buf[5] = fn_switch, buf[6] = sleep_time (0=no,1=1min,2=5min,3=30min),
          buf[8] = key_response_time, AA55 at buf[0x3e..0x3f].

        Note: the keyboard does not expose (through this path) a way to read
        the current settings, so the other fields are passed in to avoid
        overwriting them.
        """
        if not self.cmd([PROTO_MARK, CMD_OPEN]):
            return False

        head = bytearray(0x40)
        head[0] = PROTO_MARK
        head[1] = CMD_SYSSET
        head[2] = 1                        # src[2] = 1
        head[8] = 1                        # src[8] = 1
        if not self.cmd(bytes(head)):
            return False

        pkt = bytearray(0x40)
        pkt[0] = 0x00
        pkt[1] = 0x01
        pkt[5] = fn_switch & 0xFF
        pkt[6] = value & 0xFF
        pkt[8] = key_respond & 0xFF
        pkt[0x3E] = 0xAA
        pkt[0x3F] = 0x55
        self.cmd(bytes(pkt), wait_ack=False)   # driver sends this without ACK
        time.sleep(0.2)                        # keyboard processes before save
        return self.cmd([PROTO_MARK, CMD_SAVE], retries=5)


# ----------------------------------------------------------------- UI utils


def _color(value):
    value = value.lstrip("#")
    if len(value) != 6:
        raise argparse.ArgumentTypeError("color must be RRGGBB (e.g. FF8800)")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def _hex(data, width=16):
    for i in range(0, len(data), width):
        chunk = data[i:i + width]
        print(f"  {i:02x}: " + " ".join(f"{b:02x}" for b in chunk))


# ---------------------------------------------------------------- commands


def do_list(_args):
    devs = list(iter_hidraw())
    if not devs:
        print("No hidraw devices found (is the hidraw module loaded?).")
        return 1
    print(f"{'DEV':<12} {'VID:PID':<10} {'IF':<3} description")
    for path, vid, pid, iface in devs:
        tag = "  <-- AJAZZ" if vid == VID_SONIX else ""
        desc = PID_DEVICES.get(pid, "") if vid == VID_SONIX else ""
        print(f"{path:<12} {vid:04x}:{pid:04x}  {iface:<3} {desc}{tag}")
    return 0


def _probe_one(path):
    """Test the 04 18 + ACK handshake. True if the interface speaks it."""
    import errno
    try:
        with AjazzKeyboard(path) as kb:
            return kb.cmd([PROTO_MARK, CMD_OPEN], wait_ack=True, retries=2)
    except PermissionError:
        print(f"  {path}: permission denied - install "
              "99-ajazz-ak820-pro.rules or use sudo")
        return None
    except OSError as exc:
        if exc.errno in (errno.EPIPE, errno.EINVAL, errno.ENODEV):
            return False   # interface exists but rejects this feature report
        print(f"  {path}: I/O error ({exc})")
        return None


def do_probe(_args):
    cands = find_candidates()
    if not cands:
        print("No Sonix (0c45) device found. Keyboard connected via USB/2.4G?")
        return 1
    found = False
    for path, vid, pid, iface in cands:
        print(f"testing {path} ({vid:04x}:{pid:04x} if={iface}) ... ",
              end="", flush=True)
        ok = _probe_one(path)
        if ok:
            print("ACK! this is the configuration interface")
            found = True
        elif ok is False:
            print("no ACK")
        if not found and ok:
            found = True
    return 0 if found else 1


def _open_first(args):
    cands = find_candidates()
    if getattr(args, "device", None):
        return AjazzKeyboard(args.device)
    probed = []
    for path, vid, pid, iface in cands:
        if _probe_one(path):
            probed.append(path)
    if not probed:
        raise SystemExit("No AJAZZ interface answered. "
                         "Run 'probe' and/or use --device.")
    return AjazzKeyboard(probed[0])


def do_light(args):
    with _open_first(args) as kb:
        print(f"writing light: mode={args.mode} color={args.color} "
              f"brightness={args.brightness} speed={args.speed}")
        ok = kb.set_light(mode=args.mode, color=args.color,
                          brightness=args.brightness, speed=args.speed,
                          direction=args.direction, colorful=args.colorful)
        print("OK - applied to the keyboard" if ok
              else "FAILED - keyboard did not acknowledge (ACK)")
        return 0 if ok else 1


def do_clock(args):
    """Sync the TFT clock with the host clock."""
    with _open_first(args) as kb:
        ok = kb.sync_clock(index=getattr(args, "index", 0))
        t = time.localtime()
        when = time.strftime("%Y-%m-%d %H:%M:%S", t)
        print(f"clock {'sent' if ok else 'FAILED (no ACK)'} -> {when}")
        return 0 if ok else 1


def do_sleep(args):
    """Set the firmware Sleep Time (backlight off after idle)."""
    with _open_first(args) as kb:
        ok = kb.set_sleep(args.value, fn_switch=args.fn_switch,
                          key_respond=args.key_respond)
        desc = SLEEP_MODES.get(args.value, f"value {args.value}")
        print(f"sleep time {'sent' if ok else 'FAILED (no ACK)'} -> {desc}")
        return 0 if ok else 1


def do_raw(args):
    if args.wire:
        wire = bytes(int(b, 16) for b in args.wire)
        payload = wire[1:]          # cmd() adds the report ID back
    else:
        payload = bytes(int(b, 16) for b in args.payload)
        if not payload:
            raise SystemExit("provide the bytes (e.g. 04 18) or use --wire")
    with _open_first(args) as kb:
        ok = kb.cmd(payload, wait_ack=args.ack)
        print("ACK" if ok else ("no ACK" if args.ack else "sent"))
        if args.read:
            resp = kb.get_feature(kb._wire(payload))
            print("response:")
            _hex(resp)
    return 0


def do_probedump(args):
    """Send 04 18 and show what the keyboard returns - for field mapping."""
    with _open_first(args) as kb:
        for payload in ([PROTO_MARK, CMD_OPEN],
                        [PROTO_MARK, CMD_SECTION, 0, 0, 0, 0, 0, 0, 1]):
            wire = kb._wire(payload)
            kb.set_feature(wire)
            resp = kb.get_feature(wire)
            print(f"cmd {' '.join(f'{b:02x}' for b in payload)} "
                  f"-> ACK={resp[4] == ACK_OK}")
            _hex(resp)
    return 0


# ------------------------------------------------------------------- main


def do_serve(args):
    """Start the web UI (server.py) - real-time lighting feedback."""
    from server import serve
    return serve(port=args.port, host=args.host) or 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="fn", required=True)

    sub.add_parser("list", help="list hidraw interfaces").set_defaults(fn=do_list)
    sub.add_parser("probe", help="find the interface that speaks the protocol").set_defaults(fn=do_probe)
    sub.add_parser("dump", help="send 04 18/04 13 and dump the response").set_defaults(fn=do_probedump)

    lg = sub.add_parser("light", help="configure the lighting")
    lg.add_argument("--mode", type=int, default=1, help="light mode (see LIGHT_MODES)")
    lg.add_argument("--color", type=_color, default=(0xFF, 0x88, 0x00), metavar="RRGGBB")
    lg.add_argument("--brightness", type=int, default=5, choices=range(0, 6))
    lg.add_argument("--speed", type=int, default=3, choices=range(0, 6))
    lg.add_argument("--direction", type=int, default=0)
    lg.add_argument("--colorful", type=int, default=0)
    lg.add_argument("--device", help="e.g. /dev/hidraw3 (default: auto)")
    lg.set_defaults(fn=do_light)

    rw = sub.add_parser("raw", help="send a raw command")
    rw.add_argument("payload", nargs="*", help="hex bytes without the report ID (e.g. 04 18)")
    rw.add_argument("--wire", nargs="*", help="full hex report (e.g. 00 04 18)")
    rw.add_argument("--ack", action="store_true", help="wait for an ACK and report it")
    rw.add_argument("--read", action="store_true", help="read the feature report after sending")
    rw.add_argument("--device")
    rw.set_defaults(fn=do_raw)

    cl = sub.add_parser("clock", help="set the TFT screen date/time")
    cl.add_argument("--index", type=int, default=0, help="screen item index (default 0)")
    cl.add_argument("--device")
    cl.set_defaults(fn=do_clock)

    sl = sub.add_parser("sleep", help="firmware Sleep Time (backlight off after idle)")
    sl.add_argument("value", type=int, choices=sorted(SLEEP_MODES),
                    help="0=no sleep, 1=1min, 2=5min, 3=30min")
    sl.add_argument("--fn-switch", type=int, default=0, choices=(0, 1),
                    help="fn_switch field (leave 0 if unused)")
    sl.add_argument("--key-respond", type=int, default=0,
                    help="key response time (leave 0 if unused)")
    sl.add_argument("--device")
    sl.set_defaults(fn=do_sleep)

    sv = sub.add_parser("serve", help="local web UI")
    sv.add_argument("--port", type=int, default=8642)
    sv.add_argument("--host", default="127.0.0.1")
    sv.set_defaults(fn=do_serve)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except PermissionError:
        print("Permission denied on /dev/hidraw*. Install "
              "99-ajazz-ak820-pro.rules or use sudo.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
