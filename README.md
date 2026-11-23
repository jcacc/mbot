# mbot

Web control for a Makeblock mBot2 (CyberPi controller), tethered over USB.

```
browser ⇄ WebSocket ⇄ server.py (aiohttp) ⇄ USB serial ⇄ CyberPi ⇄ mBot2 base
```

## Run

```sh
python3 -m venv .venv && .venv/bin/pip install pyserial aiohttp
.venv/bin/python server.py                 # http://127.0.0.1:8088
.venv/bin/python server.py --host 0.0.0.0  # LAN/phone access — no auth, home network only
```

Turn the mBot2 base on (power switch on its side). With only USB power the CyberPi
boots, but motors and sensors are absent (`get_shield()` returns `'none'`) and
reads on them hang. The page shows a banner in that state.

## Files

- `cyberlink.py` — minimal client for the CyberPi "online mode" serial protocol: framed
  MicroPython expressions evaluated on the device, `{"ret": ...}` replies. Replaces
  Makeblock's `makeblock`/`cyberpi` PyPI packages.
- `server.py` — robot thread (drive, actions, telemetry) + aiohttp WebSocket server.
- `static/index.html` — control page: touch joystick / WASD, speed, STOP, sensors, LEDs, beeps, screen text.
- `probe.py` — evaluate expressions on the robot: `.venv/bin/python probe.py "cyberpi.get_battery()"`.

## Gotchas

- **DTR/RTS:** macOS asserts both on open, which holds the ESP32 in reset (silent port).
  `cyberlink` opens with both released and waits ~3 s for boot.
- **Errors are silent:** an expression that raises on the device gets no reply; you just see a timeout.
- **Motor direction:** EM2 is mirrored, so forward = `mbot2.drive_speed(v, -v)` (`RIGHT_SIGN` in server.py).
- **Safety:** the server stops the motors when drive commands go quiet for 0.5 s or the last browser disconnects.
- Firmware seen: 44.01.006 (Mar 2021).
