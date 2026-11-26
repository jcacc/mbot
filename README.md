# mbot

Web control for a [Makeblock](https://github.com/Makeblock-official) mBot2 (CyberPi controller), over USB or Wi-Fi.
Drive it from a browser or phone with a touch joystick, WASD or a game controller, and watch its sensors live.

Not affiliated with Makeblock. The serial protocol was worked out from Makeblock's
[`makeblock`](https://pypi.org/project/makeblock/) / [`cyberpi`](https://pypi.org/project/cyberpi/)
Python packages and by probing the robot; no Makeblock code is included.

```
USB:    browser ⇄ WebSocket ⇄ server.py ⇄ USB serial ⇄ CyberPi ⇄ mBot2 base
Wi-Fi:  browser ⇄ WebSocket ⇄ server.py ⇄ TCP :8096 ⇄ robot/mbot_wifi.py on the CyberPi
```

## Run (USB)

```sh
python3 -m venv .venv && .venv/bin/pip install pyserial aiohttp
.venv/bin/python server.py                 # http://127.0.0.1:8088
.venv/bin/python server.py --host 0.0.0.0  # LAN/phone access — no auth, home network only
```

Turn the mBot2 base on (power switch on its side). With only USB power the CyberPi
boots, but motors and sensors are absent (`get_shield()` returns `'none'`) and
reads on them hang. The page shows a banner in that state.

**Base not detected?** The CyberPi only checks for the base when it boots. Switch the
base off and on, then press **Re-detect base** on the page (the server closes the
port and reopens it 3 s later, which reboots the CyberPi).

No driver needed on macOS: the CH340 USB-serial chip uses the built-in driver
(`/dev/cu.usbserial-*`).

## Run (Wi-Fi)

The robot connects **out** to the server, so the server needs a fixed address on your LAN.

1. **Server:** `MBOT_TOKEN=<random secret> .venv/bin/python server.py --host 0.0.0.0 --port 8097 --robot-port 8096`
   (only `aiohttp` is needed; `deploy/mbot.service` runs it under systemd with the token in `.env`).
2. **Robot:** open `robot/mbot_wifi.py`, fill in `WIFI_SSID`, `WIFI_PASS` (2.4 GHz network; the ESP32
   can't see 5 GHz), `SERVER`, and `TOKEN`. In mBlock 5, connect the CyberPi, switch the editor to
   **Python**, choose **Upload** mode, paste the file, and upload. It runs at power-on.
3. Switch the robot on. Its screen shows Wi-Fi → Connected, the LEDs go green, and the page shows
   *robot linked · Wi-Fi*.

The robot stops its own motors if drive commands stop for 0.6 s, so losing Wi-Fi can't leave it driving.

## Game controller

Any controller the browser can see works (Bluetooth or USB: Xbox, PS4/PS5, Switch Pro, 8BitDo…).
Pair it to the device that has the control page open, then press a button on it: browsers only
report a controller after a button press on that page. A green **controller** badge shows when it's picked up.

| Input | Does |
|---|---|
| Left stick / D-pad | drive (forward/back + turn) |
| RT / LT | throttle forward / reverse; the left stick steers |
| A (Cross) | beep |
| B (Circle) | stop |
| X (Square) | next LED colour |
| Y (Triangle) | LEDs off |
| LB / RB | speed −/+ 10 |

Releasing the stick or losing the controller sends a stop straight away. Driving forward within
15 cm of something gives a short rumble (Chrome/Edge). Button mapping needs a controller the
browser reports as `standard`; anything else only gets the left stick.

Use Chrome or Edge on the plain-http LAN URL; they still expose controllers there
(Chrome 158 checked), but Firefox may hide them outside https/localhost.

## Scratch blocks (TurboWarp)

`scratch/mbot2.js` is a [TurboWarp](https://turbowarp.org) custom extension that adds mBot2 blocks:
drive (forward/backward/left/right, for N seconds, or per-wheel RPM), stop, LEDs, beep, screen text,
and reporters for distance, line sensor, battery, roll/pitch, light, loudness, and wheel speed. It
speaks the same WebSocket messages as the control page, so it works over USB or Wi-Fi.

- It must run **unsandboxed** (it needs `Scratch.vm` to stop the robot on the stop sign). Stock
  TurboWarp only runs *URL*-loaded extensions unsandboxed from its trusted list (the sandbox checkbox
  is greyed out on the URL tab), and a sandboxed load fails with `encountered runtime error: Script error.`.
  Two ways round it:
  - **Self-hosted editor patched to trust its own `ext/` folder** (what lookingGlass runs; the patch is
    `scratch/turbowarp-trust-local-ext.patch` in the personal monorepo). Put `mbot2.js` at
    `<editor dir>/ext/mbot2.js` and load **Extensions → Custom Extension → URL** with that URL, using
    the same hostname as the page. No checkbox is needed, and saved projects reload it automatically.
  - **Stock TurboWarp:** use the **Files** (or Text) tab instead of URL, tick "Run extension without
    sandbox", and pick `mbot2.js`.
- By default it connects to `ws://<page host>/mbot/ws`, so serve the editor and the extension from
  the same plain-http host and reverse-proxy `/mbot/` to the server (with WebSocket upgrade headers).
  The `connect to [URL]` block overrides this, e.g. `ws://127.0.0.1:8088/ws` for USB mode.
- **The editor page has to be http or localhost.** Browsers block `ws://` from an https page (mixed
  content), so turbowarp.org itself can't reach a LAN robot.
- Safety: it resends the drive target every 100 ms (the server stops after 0.5 s without one). The
  stop sign, closing the tab, and a dropped socket all stop the robot.

## Files

- `cyberlink.py` — minimal client for the CyberPi "online mode" serial protocol: framed
  MicroPython expressions evaluated on the device, `{"ret": ...}` replies. Replaces
  Makeblock's `makeblock`/`cyberpi` PyPI packages.
- `server.py` — aiohttp WebSocket server; USB backend (serial thread) or Wi-Fi backend (`--robot-port`).
- `robot/mbot_wifi.py` — program for the CyberPi in Wi-Fi mode (upload with mBlock).
- `deploy/mbot.service` — systemd unit for running the Wi-Fi server on a Pi.
- `static/index.html` — control page: touch joystick / WASD / game controller, speed, STOP, sensors, LEDs, beeps, screen text.
- `scratch/mbot2.js` — TurboWarp extension (mBot2 blocks over the server's WebSocket).
- `probe.py` — evaluate expressions on the robot: `.venv/bin/python probe.py "cyberpi.get_battery()"`.

## Gotchas

- **DTR/RTS:** macOS asserts both on open, which holds the ESP32 in reset (silent port).
  `cyberlink` opens with both released and waits ~3 s for boot.
- **Errors are silent:** an expression that raises on the device gets no reply; you just see a timeout.
- **~255-byte script limit:** longer expressions are silently dropped. `cyberlink` refuses anything over 250 bytes.
- **Avoid `mbot2.EM_stop('ALL')`:** prime suspect for the base dropping out of detection until it's
  power-cycled. Stop with `mbot2.drive_speed(0, 0)` instead.
- **Motor direction:** EM2 should be mirrored, so forward = `mbot2.drive_speed(v, -v)` (`RIGHT_SIGN` in server.py). Not yet confirmed on the floor; flip the sign if it spins in place.
- **Safety:** the server stops the motors when drive commands go quiet for 0.5 s or the last browser disconnects.
- Firmware seen: 44.01.006 (Mar 2021).
