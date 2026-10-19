#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 AJAZZ Linux contributors
"""Local web UI server for ajazz_ctl (real-time lighting control).

Usage: sudo python3 ajazz_ctl.py serve [--port 8642]

Endpoints:
  GET  /              -> UI page (static/index.html)
  GET  /api/state     -> current state + device + last ACK
  GET  /api/modes     -> lighting mode table (market names)
  GET  /api/layout    -> keyboard layout (asset or vendor XML fallback)
  POST /api/light     -> apply {mode,color,brightness,speed,direction,colorful}
                        and return {ack, tries, applied}
  POST /api/clock     -> sync the TFT clock (index)
  POST /api/sleep     -> set Sleep Time {value, fn_switch, key_respond}
  POST /raw           -> send a raw hex payload (experimental)
"""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ajazz_ctl import (AjazzKeyboard, find_candidates, _probe_one,
                       LIGHT_MODES, SLEEP_MODES, PROTO_MARK)

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(ROOT, "static")
# Derived layout (committed): safe to publish, no vendor files required.
LAYOUT_JSON = os.path.join(ROOT, "assets", "ak820-pro-layout.json")
# Fallback for local development: vendor XML from the extracted installer.
LAYOUT_XML = os.path.join(ROOT, "temp", "extracted", "app", "layouts",
                          "rgb-keyboard1.xml")

# Keyboard layout (loaded once and cached)
_layout = None


# ---- AK820 Pro (75%) physical layout overrides ----
# The vendor XML describes the MK87 (TKL, 87 keys). The AK820 Pro has 81 keys:
# Del in the top row after F12, a Home/PgUp/PgDn column and the 0.85" TFT in
# the bottom-right corner (where End used to be), with the arrow cluster
# grouped to the left.
_AK820_POS = {
    "del":       (583,  35),   # top row, right after F12, flush with Backspace
    "home":      (647,  84),   # right of Backspace
    "page up":   (647, 122),   # below Home
    "page down": (647, 161),   # below PgUp
    "Ctrl_R":    (528, 237),   # takes the old APP key position
    "↑":         (610, 199),
    "←":         (573, 237),
    "↓":         (610, 237),
    "→":         (647, 237),
}
_AK820_REMOVE = {"insert", "end", "APP"}
_AK820_F_LEFT = 120                  # F1 (closer to Esc)
_AK820_F_STEP = (546 - 120) / 11     # F12 ends at 576; Del at 583..613


def _apply_ak820_layout(keys):
    """Convert the vendor XML layout (MK87) to the real AK820 Pro layout."""
    out = []
    for k in keys:
        if k["name"] in _AK820_REMOVE:
            continue
        if k["name"] in _AK820_POS:
            k["left"], k["top"] = _AK820_POS[k["name"]]
        if k["name"] == "Shift_R":
            k["width"] = 57          # ends where the old Ctrl started (x=575)
        name = k["name"]
        if len(name) > 1 and name[0] == "F" and name[1:].isdigit():
            n = int(name[1:])
            if 1 <= n <= 12:         # compress the Esc/F row
                k["left"] = round(_AK820_F_LEFT + (n - 1) * _AK820_F_STEP)
        out.append(k)
    # decorative items: 0.85" TFT (below PgDn) and the knob (above Home)
    out.append({"name": "TFT", "code": "", "key_index": 0, "light_index": 0,
                "row": 4, "col": 17, "left": 647.0, "top": 199.0,
                "width": 30.0, "height": 30.0, "kind": "screen"})
    out.append({"name": "knob", "code": "", "key_index": 0, "light_index": 0,
                "row": 0, "col": 17, "left": 647.0, "top": 35.0,
                "width": 30.0, "height": 30.0, "kind": "knob"})
    return out


def _load_layout_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_layout_xml(path):
    import xml.etree.ElementTree as ET
    out = {"width": 800.0, "height": 300.0, "bk_color": "#CD7A00", "keys": []}
    root = ET.parse(path).getroot()
    kb = root.find(".//Keyboard")
    if kb is not None:
        out["width"] = float(kb.get("width") or 800)
        out["height"] = float(kb.get("height") or 300)
        out["bk_color"] = "#" + (kb.get("bk_color") or "CD7A00")
        for el in kb.findall(".//key"):
            rc = (el.get("row_col") or "0#0").split("#")
            out["keys"].append({
                "name": el.get("name") or el.get("desc") or "",
                "code": el.get("code") or "",
                "key_index": int(el.get("key_index") or 0),
                "light_index": int(el.get("light_index") or 0),
                "row": int(rc[0]),
                "col": int(rc[1]) if len(rc) > 1 else 0,
                "left": float(el.get("rect_left") or 0),
                "top": float(el.get("rect_top") or 0),
                "width": float(el.get("rect_width") or 30),
                "height": float(el.get("rect_height") or 30),
            })
    out["keys"] = _apply_ak820_layout(out["keys"])
    return out


def load_layout():
    """Load the keyboard layout (derived JSON first, vendor XML as fallback)."""
    global _layout
    if _layout is not None:
        return _layout
    try:
        _layout = _load_layout_json(LAYOUT_JSON)
        return _layout
    except (OSError, ValueError):
        pass
    try:
        _layout = _load_layout_xml(LAYOUT_XML)
    except Exception as exc:   # noqa: BLE001
        _layout = {"width": 800.0, "height": 300.0, "keys": [],
                   "error": str(exc)}
    return _layout


# Shared state between requests (the HTTP server is threaded -> lock the HID)
_lock = threading.Lock()
_state = {
    "device": None,
    "mode": 1,
    "color": "#FF8800",
    "brightness": 5,
    "speed": 3,
    "direction": 0,
    "colorful": 0,
    "last_ack": None,      # None = not tested yet; True/False = last result
    "last_error": None,
    "last_applied": None,  # timestamp of the last accepted request
    "clock": None,         # last TFT clock sync
    "sleep": None,         # last applied sleep time (0..3)
    "fn_switch": 0,
    "key_respond": 0,
}

_kb = None                 # persistent HID session


def _get_kb():
    """Open/reopen the HID session; return None if no interface answers."""
    global _kb
    if _kb is not None:
        try:
            os.fstat(_kb.fd)      # is the device still there?
            return _kb
        except OSError:
            _kb = None
    for path, vid, pid, iface in find_candidates():
        try:
            if _probe_one(path):
                _kb = AjazzKeyboard(path)
                _state["device"] = path
                return _kb
        except OSError:
            continue
    return None


def apply_light(payload, retries=3):
    """Apply lighting with retries until ACK. Returns (ack, tries, error)."""
    mode = int(payload.get("mode", 1))
    color = payload.get("color", "#FF8800")
    if isinstance(color, str):
        color = color.lstrip("#")
        color = tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))
    kwargs = dict(
        mode=mode,
        color=color,
        brightness=int(payload.get("brightness", 5)),
        speed=int(payload.get("speed", 3)),
        direction=int(payload.get("direction", 0)),
        colorful=int(payload.get("colorful", 0)),
    )
    tries, last_err = 0, None
    for attempt in range(1, retries + 1):
        tries = attempt
        kb = _get_kb()
        if kb is None:
            last_err = "no hidraw interface answered"
            time.sleep(0.1)
            continue
        try:
            ok = kb.set_light(**kwargs)
        except OSError as exc:
            last_err = f"I/O error: {exc}"
            time.sleep(0.1)
            continue
        if ok:
            _state.update({k: v for k, v in
                           [("mode", kwargs["mode"]),
                            ("color", "#%02X%02X%02X" % kwargs["color"]),
                            ("brightness", kwargs["brightness"]),
                            ("speed", kwargs["speed"]),
                            ("direction", kwargs["direction"]),
                            ("colorful", kwargs["colorful"])]})
            _state["last_ack"] = True
            _state["last_error"] = None
            _state["last_applied"] = time.time()
            return True, tries, None
        last_err = "keyboard did not acknowledge (ACK)"
    _state["last_ack"] = False
    _state["last_error"] = last_err
    return False, tries, last_err


def apply_clock(index=0):
    """Sync the keyboard screen clock with the host clock."""
    tries, last_err = 0, None
    for attempt in range(1, 4):
        tries = attempt
        kb = _get_kb()
        if kb is None:
            last_err = "no hidraw interface answered"
            time.sleep(0.1)
            continue
        try:
            ok = kb.sync_clock(index=int(index))
        except OSError as exc:
            last_err = f"I/O error: {exc}"
            time.sleep(0.1)
            continue
        if ok:
            _state["last_ack"] = True
            _state["clock"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _state["last_error"] = None
            return True, tries, None
        last_err = "keyboard did not acknowledge (ACK)"
    _state["last_ack"] = False
    _state["last_error"] = last_err
    return False, tries, last_err


def apply_sleep(value, fn_switch=0, key_respond=0):
    """Set the firmware Sleep Time (backlight/screen idle timeout).

    Only effective in wireless mode (BT/2.4G); on USB the host polls
    constantly and the keyboard never goes idle.
    """
    value = int(value)
    tries, last_err = 0, None
    for attempt in range(1, 4):
        tries = attempt
        kb = _get_kb()
        if kb is None:
            last_err = "no hidraw interface answered"
            time.sleep(0.1)
            continue
        try:
            ok = kb.set_sleep(value, fn_switch=int(fn_switch),
                              key_respond=int(key_respond))
        except OSError as exc:
            last_err = f"I/O error: {exc}"
            time.sleep(0.1)
            continue
        if ok:
            _state.update({"sleep": value, "fn_switch": int(fn_switch),
                           "key_respond": int(key_respond)})
            _state["last_ack"] = True
            _state["last_error"] = None
            _state["last_applied"] = time.time()
            return True, tries, None
        last_err = "keyboard did not acknowledge (ACK)"
    _state["last_ack"] = False
    _state["last_error"] = last_err
    return False, tries, last_err


def apply_raw(hexbytes, ack=True):
    payload = bytes(int(b, 16) for b in hexbytes.split())
    kb = _get_kb()
    if kb is None:
        return False, "no interface answered"
    try:
        ok = kb.cmd(payload, wait_ack=ack)
    except OSError as exc:
        return False, f"I/O error: {exc}"
    _state["last_ack"] = ok if ack else None
    return True, "ACK" if ok else "no ACK"


class Handler(BaseHTTPRequestHandler):
    server_version = "ajazz_ctl/1.0"

    def log_message(self, fmt, *args):
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # ---- helpers ----

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _page(self, status=200):
        path = os.path.join(STATIC_DIR, "index.html")
        try:
            body = open(path, "rb").read()
        except OSError:
            body = b"<h1>static/index.html not found</h1>"
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    # ---- routes ----

    def do_GET(self):
        if self.path == "/api/layout":
            return self._json(load_layout())
        with _lock:
            if self.path == "/api/state":
                return self._json(dict(_state, modes=LIGHT_MODES,
                                       sleep_modes=SLEEP_MODES))
            if self.path == "/api/modes":
                return self._json(LIGHT_MODES)
        if self.path in ("/", "/index.html"):
            return self._page()
        self._json({"error": "unknown route"}, 404)

    def do_POST(self):
        try:
            data = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "invalid JSON"}, 400)
        with _lock:
            if self.path == "/api/light":
                ack, tries, err = apply_light(data)
                return self._json(dict(_state, ack=ack, tries=tries, error=err))
            if self.path == "/raw":
                ok, msg = apply_raw(data.get("payload", ""),
                                    ack=bool(data.get("ack", True)))
                return self._json({"ok": ok, "msg": msg})
            if self.path == "/api/clock":
                ack, tries, err = apply_clock(data.get("index", 0))
                return self._json(dict(_state, ack=ack, tries=tries, error=err))
            if self.path == "/api/sleep":
                ack, tries, err = apply_sleep(data.get("value", 0),
                                              data.get("fn_switch", 0),
                                              data.get("key_respond", 0))
                return self._json(dict(_state, ack=ack, tries=tries, error=err))
        self._json({"error": "unknown route"}, 404)


def serve(port=8642, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ajazz_ctl serve -> http://{host}:{port}/")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down...")
    finally:
        httpd.server_close()
        global _kb
        if _kb is not None:
            _kb.close()
