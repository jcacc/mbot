# mBot2 Wi-Fi client: runs ON the CyberPi (upload with mBlock 5 -> Python -> Upload mode).
#
# Joins Wi-Fi, connects OUT to the mbot server (server.py --robot-port), applies
# drive/LED/sound/screen commands, and streams telemetry. Stops the motors by
# itself if drive commands stop arriving, so a dropped link can't leave it driving.
#
# Protocol (one ASCII line each, "\n"-terminated):
#   robot -> server  "H <token>"                hello, first line after connecting
#                    "T d,line,batt,roll,pitch,em1,em2,light,loud,shield"
#   server -> robot  "D <left_rpm> <right_rpm>"  drive (repeated ~10x/s while moving)
#                    "L <r> <g> <b>" / "O"       LEDs on / off
#                    "B <freq>"                  beep
#                    "S <text>"                  text on the screen
#                    "P"                         keepalive

import cyberpi
import mbot2
import usocket
import utime

# ---- config (fill in before uploading) ----------------------------------------
WIFI_SSID = "your-2.4GHz-ssid"
WIFI_PASS = "your-wifi-password"
SERVER = "192.168.10.22"   # lookingGlass
PORT = 8096
TOKEN = "change-me"         # must match MBOT_TOKEN on the server
# -------------------------------------------------------------------------------

RIGHT_SIGN = -1            # EM2 is mirrored: forward = drive_speed(+v, -v)
WATCHDOG_MS = 600          # stop if no drive command for this long
TELEMETRY_MS = 200
MAX_RPM = 200


def show(*lines):
    cyberpi.console.clear()
    for line in lines:
        cyberpi.console.println(str(line))


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def stop():
    mbot2.drive_speed(0, 0)


def wifi_up():
    if cyberpi.wifi.is_connect():
        return
    show("WiFi:", WIFI_SSID)
    cyberpi.led.on(255, 120, 0)
    cyberpi.wifi.connect(WIFI_SSID, WIFI_PASS)
    t0 = utime.ticks_ms()
    while not cyberpi.wifi.is_connect():
        if utime.ticks_diff(utime.ticks_ms(), t0) > 20000:
            show("WiFi failed", "retrying...")
            cyberpi.wifi.connect(WIFI_SSID, WIFI_PASS)
            t0 = utime.ticks_ms()
        utime.sleep_ms(250)


def telemetry():
    shield = cyberpi.get_shield()
    if shield == "mbot2":
        v = (cyberpi.ultrasonic2.get(1), cyberpi.quad_rgb_sensor.get_line_sta(1),
             cyberpi.get_battery(), cyberpi.get_roll(), cyberpi.get_pitch(),
             mbot2.EM_get_speed("EM1"), mbot2.EM_get_speed("EM2"),
             cyberpi.get_bri(), cyberpi.get_loudness("maximum"))
    else:
        v = ("", "", "", "", "", "", "", cyberpi.get_bri(), cyberpi.get_loudness("maximum"))
    return "T " + ",".join(str(x) for x in v) + "," + str(shield) + "\n"


def handle(line, state):
    parts = line.split(" ", 1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    if cmd == "D":
        l, r = arg.split()
        l = int(clamp(int(l), -MAX_RPM, MAX_RPM))
        r = int(clamp(int(r), -MAX_RPM, MAX_RPM))
        state["last_drive"] = utime.ticks_ms()
        if (l, r) != state["sent"]:
            mbot2.drive_speed(l, RIGHT_SIGN * r)
            state["sent"] = (l, r)
    elif cmd == "L":
        r, g, b = (int(clamp(int(x), 0, 255)) for x in arg.split())
        cyberpi.led.on(r, g, b)
    elif cmd == "O":
        cyberpi.led.off()
    elif cmd == "B":
        cyberpi.audio.play_tone(int(clamp(int(arg), 100, 4000)), 0.15)
    elif cmd == "S":
        show(arg[:40])


def session():
    """One connection to the server; returns when it drops."""
    addr = usocket.getaddrinfo(SERVER, PORT)[0][-1]
    s = usocket.socket()
    s.settimeout(5)
    s.connect(addr)
    s.settimeout(0.02)
    s.send(("H " + TOKEN + "\n").encode())
    show("Connected", SERVER)
    cyberpi.led.on(0, 120, 0)
    state = {"last_drive": 0, "sent": (0, 0)}
    buf = b""
    next_tel = utime.ticks_ms()
    last_rx = utime.ticks_ms()
    try:
        while True:
            now = utime.ticks_ms()
            try:
                chunk = s.recv(256)
                if not chunk:
                    return  # server closed
                buf += chunk
                last_rx = now
            except OSError:
                pass  # timeout: nothing to read
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    handle(line.decode().strip(), state)
                except Exception:
                    pass  # ignore malformed commands
            if state["sent"] != (0, 0) and utime.ticks_diff(now, state["last_drive"]) > WATCHDOG_MS:
                stop()
                state["sent"] = (0, 0)
            if utime.ticks_diff(now, last_rx) > 5000:
                return  # server silent (it pings every second)
            if utime.ticks_diff(now, next_tel) >= 0:
                next_tel = utime.ticks_add(now, TELEMETRY_MS)
                s.send(telemetry().encode())
    finally:
        stop()
        s.close()


def main():
    stop()
    while True:
        try:
            wifi_up()
            session()
            show("Server lost", "reconnecting...")
        except Exception as e:
            stop()
            show("Error:", str(e)[:30], "retrying...")
        cyberpi.led.on(255, 0, 0)
        utime.sleep(2)


main()
