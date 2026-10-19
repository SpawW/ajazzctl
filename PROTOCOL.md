# HID protocol — AJAZZ RGB keyboards (vendor driver analysis)

> Reverse-engineered from the official Windows installer
> (`driver.exe`, Inno Setup 6.1.0, "AJAZZ ... RGB Keyboard Setup",
> software beta 1.0.0.1, copyright Shenzhen A-Jazz).
> Static analysis with `pefile` + `capstone`; scripts in `tools/`.
> Tested on an **AJAZZ AK820 Pro** (75%, 81 keys + knob + 0.85" TFT).

## 1. Key conclusion

**There is no kernel driver.** The "driver" is a user-mode Windows application
(MFC/C++) that talks to the keyboard through the standard HID API:

- `HidD_SetFeature` (loaded dynamically from `hid.dll`) → **65-byte** feature reports
- `DeviceIoControl(IOCTL_HID_GET_FEATURE = 0x000B0192)` + `ReadFile` → read (ACK)
- `WriteFile` + `ReadFile` → **33-byte** output reports with a checksum (read path)

On Linux this maps directly to **hidraw** (`HIDIOCSFEATURE` / `HIDIOCGFEATURE`);
no kernel module is required.

## 2. USB identifiers (from `config.xml`)

| Mode | VID | PID | HID interface | Product |
|------|-----|-----|---------------|---------|
| USB | `0C45` | `8006` | MI_00 | AJAZZ keyboard |
| USB | `0C45` | `8009` | MI_00 | AJAZZ AK820 Pro (TFT) |
| 2.4 GHz | `0C45` | `FEFE` | MI_03 | 2.4G dongle |

- `0x0C45` = Sonix Technology (the keyboard's MCU vendor).
- **Bluetooth does not support configuration** (the vendor app itself says so;
  string 62 of `language/1033.lan`). BT HID carries only standard reports.

## 3. Transport layer

### 3.1 65-byte feature report (write)

The driver builds a 64-byte `src` buffer and sends it via
`HidD_SetFeature(handle, dst, 0x41)`, where `dst[0] = 0x00` (report ID) and
`dst[1..64] = src[0..63]`.

On the wire (65 bytes):

```
byte  0     : 0x00            (report ID)
byte  1     : 0x04            (protocol marker, present in every control command)
byte  2     : CMD             (command)
byte  3..64 : payload
```

### 3.2 Read / ACK

After a `SetFeature`, for commands that expect confirmation, the driver calls
`DeviceIoControl(IOCTL_HID_GET_FEATURE)` on the same buffer and checks:

```
response[4] == 0x01   ->  ACK (command accepted by the keyboard)
```

The driver retries (reopening the device) on failure. **ACKs are flaky by
design** — always retry before concluding that a command failed.

### 3.3 33-byte output report (secondary path)

Function `0x4f4d0`: computes **checksum = sum of bytes [0..len-1]**, stores it in
byte `[0x20]`, sends 33 bytes via `WriteFile` and reads the reply with
`ReadFile`. Used by configuration read flows; not used by the lighting apply.

## 4. Canonical write ("apply") sequence

Every configuration write follows this script (from functions `0x141a0`,
`0x2b000`, `0x33fd0`, `0x2d600`, `0x4b813`):

```
1. [00] 04 18                          -> open configuration session (ACK)
2. [00] 04 13 00 00 00 00 00 00 00 01  -> select write section (ACK)
   (byte 0x01 at src[8] = wire[9])
3. [00] <data packet> ... AA 55        -> data (no ACK)
4. [00] 04 02                          -> save (ACK)
5. [00] 04 F0                          -> apply/exit (no ACK)
```

The `AA 55` magic marks the end of the data packet (position depends on the
command: `wire[0x0f..0x10]` for lighting, `wire[0x3f..0x40]` for general
settings).

## 5. Command table (CMD in byte 2)

Found by scanning `mov word [buf], 0xNN04` in the binary:

| CMD | Likely function | Evidence (RVA) |
|-----|-----------------|----------------|
| `0x02` | Save / close write stage | `0x143ad`, `0x2b0ef`, `0x3406e` |
| `0x05` | Query (firmware version?) | `0x4f88d` |
| `0x13` | Select write section (arg at `[8]`) | `0x2b05e`, `0x34012` |
| `0x15` | Per-key custom light (block write) | `0x2dab3` |
| `0x17` | System settings (sleep, fn, ...) | `0x141ff` (USB), `0x14410` (2.4G) |
| `0x18` | Open configuration session | `0x141e0`, `0x2b03f` |
| `0x19` | Prepare per-key custom light (key list) | `0x2d63b` |
| `0x20` | Fn layer / other options | `0x27fb4`, `0x32914` |
| `0x23` | Key remapping / macros | `0x4b99b` |
| `0x28` | Screen: clock sync (`src[8]=1`) | `0x23acd` |
| `0x72` | Screen: image/GIF upload (4 KB blocks) | `0x2306d` |
| `0xF0` | Apply and exit | `0x18d34`, `0x2b119` |

Data packets (no `0x04` prefix), with `AA55` magic:

- `{MODE, R, G, B, 0, 0, 0, 0, b8, b9, b10, b11, 0, 0, AA, 55}` → **lighting** (wire)
- `{00, 01, ..., AA55 at offset 0x3e}` → general settings (profiles etc.)

## 6. Lighting packet

From the constructor at `RVA 0x2b000` (64-byte `src`; this data packet has **no**
`0x04` prefix and is sent right after `04 13`):

```
src[0]      = light mode (confirmed on hardware, 0..15)
src[1]      = R  (low byte of COLORREF 0x00BBGGRR)
src[2]      = G
src[3]      = B
src[8]      = colorful: 0 = single color (uses src[1..3]), >0 = built-in rainbow  ✅
src[9]      = brightness 0..5 (0 = LEDs off)                                      ✅
src[10]     = speed 0..5 (0 = slowest, 5 = fastest)                               ✅
src[11]     = direction/inversion: 0 = normal, 2 = inverted                       ✅
src[0x0e]   = 0xAA
src[0x0f]   = 0x55
```

Wire = `[00, src[0..63]]`, so `AA55` lands at `wire[0x0f..0x10]`.

> ✅ **Bytes 8–11 order fully confirmed on hardware (2026-10):**
> `src[8]`=colorful (0=solid, >0=rainbow), `src[9]`=brightness,
> `src[10]`=speed, `src[11]`=direction. In practice `src[11]` is an
> **inversion bit**: `0`=normal (mode 11 left→right, mode 12 counter-clockwise)
> and `2`=inverted (right→left, clockwise). Values `1`/`3` are unstable and
> `4`/`5` fall back to the default; use only 0 or 2.

### Light modes — confirmed on hardware

| ID | Effect observed on the physical keyboard |
|----|------------------------------------------|
| 0  | LEDs **off** |
| 1  | Static rainbow (all on, static colors) |
| 2  | Reactive — off; on keypress the key's LED lights for ~2 s with a random per-key fixed color (blue/green, orange at the edges) |
| 3  | Random **fixed** colors per key (static christmas tree) |
| 4  | Twinkle — random keys blink slowly |
| 5  | Starlight / glittering |
| 6  | Spectrum — all on, each LED color changes across a scale |
| 7  | Breathing — slow pulse in the selected color |
| 8  | Color cycle — the whole board changes color |
| 9  | Ripple — "stone in the lake" (from the center) |
| 10 | Rainbow wave — horizontal bands |
| 11 | Diagonal wave (~45°, left→right) |
| 12 | Rotating wave (spins like a clock hand) |
| 13 | Reactive per row — color/intensity on the pressed key's row |
| 14 | Reactive ripple — wave from the pressed key |
| 15 | Reactive wave — slow, wide rainbow from the pressed key |

Market names (UI strings 521–540): Static, SingleOn, SingleOff, Glittering,
Fluttering, Colourful, Breath, Spectrum, Outward, Scrolling, Rolling, Rotating,
Explode, Launch, Ripples, Flowing, Pulsating, Tilt, Shuttle, LED Off.

Ranges (from `layouts/rgb-keyboard.xml`): `brightness_max=5`, `speed_max=5`,
`default_mode=11`, `default_brightness=5`, `default_speed=3`.

## 7. Other mapped features

- **Macros**: local SQLite in the vendor app (`t_macro_data`, `t_macrorecord`,
  ...); recorded through command `0x23`.
- **Per-key custom light**: constructor at `0x2d600` — sends `{04 19}`, builds a
  0xe00-byte buffer (key count at `src[8]` as a word; `(index, RGB)` pairs with
  `0xB0/0x30/0x90/0x10/0x50` markers), sends `{04 15, count}` + 64-byte blocks
  + `AA55`, and closes with `{04 02}`. Mirrors the
  `t_customlight_data`/`t_customrgb_data` tables.
- **TFT screen** (models with a display): GIF up to 141 frames, 240×135 px,
  256-byte header (`gif_headlength=256`); commands `0x28` (clock) and `0x72`
  (image/GIF upload).
  - **Clock `04 28`** (implemented and confirmed): `04 18` → `04 28` with
    `src[8]=1` → data packet
    `[00, gif+1, 0x5A, year%2000, month, day, hour, min, sec, 0, dow, … , AA 55]`
    (the leading `00` is mandatory — without it the fields shift by one byte and
    nothing changes, even though the command ACKs) → `04 02`.
  - The AK820 Pro uses a 0.85" TFT; the vendor XML (from the analyzed
    installer) declares 240×135. The clock command works on the AK820 Pro.
- **System settings — `04 17` (Sleep Time)** — implemented and **confirmed on
  hardware** ✅
  - This is the vendor app's "System Settings" page: `key_respondtime` (140),
    `sleep_time` (141: **No Sleep / 1 min / 5 min / 30 min**), disable
    Windows/ALT+F4/ALT+TAB keys (160–162) and `fn_switch`.
  - **USB variant** (mode 0, function RVA `0x141a0`): `04 18` → `04 17`
    (`src[2]=1`, `src[8]=1`) → data packet → `04 02`. **No** `04 13`, **no**
    `04 F0`.
  - **2.4G variant** (mode 2, function RVA `0x14410`): different data header
    (`00 07 10 00 00 01 …`). The choice is made by `[obj+0x4d8]`:
    `0`=USB, `2`=2.4G.
  - **Data packet** (USB variant; buffer seen by the keyboard):
    ```
    buf[0]      = 0x00
    buf[1]      = 0x01
    buf[2..4]   = disable Windows / ALT+F4 / ALT+TAB (0/1)
    buf[5]      = fn_switch
    buf[6]      = sleep_time   (0 = No Sleep, 1 = 1min, 2 = 5min, 3 = 30min)
    buf[8]      = key_response_time
    buf[0x3e]   = 0xAA
    buf[0x3f]   = 0x55
    ```
    Sent **without ACK** (like the driver). Note: after the data packet, `04 02`
    only ACKs after **~200 ms** (the driver's 35 ms timeout is not enough).
  - **Only effective in wireless mode (BT/2.4G).** On USB the host polls the
    keyboard constantly, so it never detects inactivity and never sleeps.
    Tested: nothing changes over USB; over **Bluetooth the backlight/screen turn
    off after the timeout and wake on keypress** ✅.
  - There is no read path for these settings, so the tool exposes `--fn-switch`
    and `--key-respond` to avoid overwriting those fields.
- **General settings / profiles**: keys in the vendor app's SQLite
  `t_config_data(profile, key, value)` (`fn_layer`, `fn_switch`, `lightmode`,
  `sleep_time`, `key_respondtime`, `sleep_light`, `userlight`, `customlight`, …).
- **Firmware update**: `app/firmware/` was **empty** in the installer
  (`version=120, file=""`).
- **Keyboard layout**: HID usage codes + light indices in
  `layouts/rgb-keyboard.xml` and `rgb-keyboard1.xml`. The AK820 Pro layout is
  derived from these and stored as `assets/ak820-pro-layout.json`.

## 8. Linux port strategy

1. `hidraw` + `HIDIOCSFEATURE(65)`/`HIDIOCGFEATURE(65)` replicate
   `SetFeature`/read exactly.
2. Detect the correct interface (vendor MI_00/MI_03) by testing the `04 18` +
   ACK handshake.
3. Port first: session (`04 18`/`04 02`/`04 F0`), lighting (section 6) and
   sleep (`0x17`).
4. Then: remapping (`0x23`), macros, per-key light (`0x19`/`0x15`) and the
   screen (`0x28`/`0x72`).
5. `ajazz_ctl.py` already implements 1–3, plus `clock`, `sleep`, a local web UI
   and a `raw` escape hatch.

## 9. Legal note

This document contains only factual protocol information obtained through
static analysis, for interoperability purposes. No vendor code, binaries or
extracted assets are distributed with this project. See `LICENSE`.
