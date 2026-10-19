# Roadmap — Linux control tool for AJAZZ RGB keyboards

## Goal

A local tool where the user can change lighting and screen settings in real
time, seeing the result on the physical keyboard, with no vendor software and
no kernel driver. Target hardware: **AJAZZ AK820 Pro** (75%, 81 keys + knob +
0.85" TFT).

## Architecture

```
browser (localhost:8642)            Python server (stdlib)
┌─────────────────────┐   fetch    ┌──────────────────────────┐
│ visual keyboard     │ ─────────▶ │ ajazz_ctl serve          │
│ mode / color / bri  │  JSON      │  http.server + ioctl     │
│ speed / direction   │ ◀───────── │  HIDIOCSFEATURE hidraw   │
└─────────────────────┘   status   └──────────────────────────┘
```

- **Zero dependencies**: `http.server` + plain `fetch()`, no framework.
- The server runs as `sudo python3 ajazz_ctl.py serve --port 8642` (hidraw is
  root-only unless the udev rule is installed).
- The HID session stays **open** between requests (partially mitigates flaky
  ACKs — automatic retries, failure reported after 3 attempts).

## Phases

### Phase 1 — Server + lighting (MVP) ✅
- [x] `ajazz_ctl serve`: HTTP server with `/`, `/api/state`, `/api/light`,
      `/api/modes`, `/raw`
- [x] Single page (`static/index.html`): mode, color + presets, brightness/speed
      sliders, direction toggle, ACK status
- [x] Automatic resend until ACK (3 attempts) — "no ACK" treated as non-fatal

### Phase 2 — Visual keyboard ✅
- [x] Draw the AK820 Pro 75% layout from `assets/ak820-pro-layout.json`
      (derived from the vendor XML; `GET /api/layout`)
- [x] Lighting preview (solid color, per-position rainbow, brightness → opacity)
- [x] Click a key to see `name` / `key_index` / `light_index` / `code`; in
      reactive modes (2/13/14/15) only the clicked key lights up (flash)
- [ ] Live key events (evdev) — later

### Phase 3 — Fine mapping (with the UI in place)
- [x] Confirm bytes 8–11: `colorful=src[8]`, `brightness=src[9]`,
      `speed=src[10]`, `direction=src[11]` (0=normal, 2=inverted) — 2026-10
- [x] Complete the 0–15 mode table with market names
- [ ] Save/load local profiles (JSON file)

### Phase 4 — TFT screen + clock
- [x] **Investigation complete (2026-10)** — findings below
- [x] Implement `clock` in the CLI and the UI (screen clock synced and
      confirmed on hardware)
- [ ] Implement `screen`/GIF upload in the CLI and the UI

#### Findings (reverse engineering of `DeviceDriver.exe`)

**Screen**: 240×135 px, up to 141 frames, GIF; pixels in **RGB565**
(2 B/px → ~64.8 KB/frame). Layout `rgb-keyboard1.xml`:
`gif_headlength=256 gif_maxframes=141 gif_count=1`.
The screen only works **wired** (`screen="1"` on USB, `screen="0"` on wireless).

**1) Set date/time — `04 28`** (function RVA `0x23a80`, uses `GetLocalTime`) — **DONE ✅**:
```
04 18                      (open session)
04 28        src[8] = 1    (screen command; 1 = clock sync)
data packet (the device sees the driver's whole buffer; fields at buf+1..):
   buf[0]  = 0x00              (data packet marker)
   buf[1]  = gif_index + 1     (selected item, GetCurSel+1)
   buf[2]  = 0x5A              (clock marker)
   buf[3]  = year % 2000
   buf[4]  = month   buf[5] = day   buf[6] = hour
   buf[7]  = min     buf[8] = sec   buf[10] = day of week (0=Sunday)
   buf[0x3e..0x3f] = AA 55
04 02                      (save)
```
⚠️ Gotcha: the leading `0x00` is mandatory (buf[0]); without it the fields are
shifted by one byte and the screen does not change (even though it ACKs).
Wireless variant (`0x23bb0`) uses `0C 10` + AA55 — not needed.

**2) Send image/GIF — `04 72`** (function RVA `0x22ac0`):
```
04 72   wire[2] = frame count, wire[8..9] = number of 4096-byte blocks (LE)
[block 0..N of 4096 B]   data = [frame_count][delay_0..delay_n] + concatenated RGB565
04 02                      (save)
```
The app converts GIF → RGB565 (`GetImageRGB565Data`), stores a per-frame delay
(byte = min(delay/2, 255)) and slices into 4 KB blocks.

**3) Content selection:** `wire[1]` of the packet = GIF index + 1.

**To test:** other `src[8]` values in `04 28` (screen brightness? clock on/off?)
— the binary only uses `src[8]=1`.

### Phase 4b — Battery saving: Sleep Time ✅
- [x] Discovery (RE): vendor app's "System Settings" → `sleep_time`
      (**No Sleep / 1 min / 5 min / 30 min**, strings 142–145), sent by cmd `0x17`
      (USB variant `0x141a0` or 2.4G `0x14410`, selected by connection mode)
- [x] Implement `ajazz_ctl sleep <0|1|2|3>` (+ `--fn-switch` / `--key-respond`
      so the other packet fields are preserved)
- [x] **Confirmed on hardware in Bluetooth mode**: when idle, the backlight/
      screen turn off after the timeout and wake on keypress. Note: `04 02`
      needs ~200 ms of wait.
- [x] Important: **on USB the sleep does not apply** (the host polls constantly,
      so the keyboard never detects inactivity) — it is a wireless-only feature.
- [x] Expose it in the web UI (No Sleep / 1 / 5 / 30 min selector) — "Sleep Time"
      card (`POST /api/sleep`)

### Phase 5 — Later (out of current scope)
- Key remapping (cmd `0x23`) with drag-and-drop on the visual keyboard
- Macros (vendor app SQLite → `0x23` packets)
- Per-key custom lighting (`0x19`/`0x15`)
- Music/sound-reactive mode (`music=1` in the layout menu)
- TFT GIF/image upload in the UI

## Reference data already mapped
- Modes 0–15 confirmed on hardware → `PROTOCOL.md` §6
- HID interface that ACKs: the vendor interface (MI_03 on the 2.4G dongle);
  automatic retries
- Color field: `src[1..3]` = R,G,B; `src[8]`=colorful (0=solid, >0=rainbow)
- Wire: `[00 04 CMD payload...]` 65 B; ACK = `resp[4]==1`; AA55 at `src[0x0e]`

## Risks
- Flaky ACKs → resend + visible status in the UI
- hidraw is root-only → `sudo` (or the included udev rule)
- Commands like `0x28` may have flash/firmware side effects → test carefully,
  never send random firmware payloads
- Vendor files are copyrighted → keep `temp/` out of the repository
