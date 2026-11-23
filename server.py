"""mBot2 web control: aiohttp + WebSocket page, robot over USB serial or Wi-Fi.

    .venv/bin/python server.py                                  # USB serial, http://127.0.0.1:8088
    MBOT_TOKEN=... .venv/bin/python server.py --robot-port 8096 \\
        --host 0.0.0.0 --port 8097                              # Wi-Fi: robot connects in

USB mode: a robot thread owns the serial link (see cyberlink.py) and evaluates
expressions on the CyberPi.

Wi-Fi mode: the CyberPi runs robot/mbot_wifi.py, joins Wi-Fi and opens a TCP
connection to --robot-port, sending "H <token>" first. Commands and telemetry are
single text lines (protocol documented in robot/mbot_wifi.py).

Either way, browser clients send drive targets ~10x/s while a control is held; if
none arrive for WATCHDOG_S the server stops the robot, and in Wi-Fi mode the robot
also stops itself if commands stop arriving.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path

from aiohttp import WSMsgType, web

log = logging.getLogger("mbot")

HERE = Path(__file__).parent
MAX_RPM = 200          # mbot2.drive_speed limit
WATCHDOG_S = 0.5       # stop if no drive command for this long
TELEMETRY_S = 0.15     # telemetry poll period (serial)
RIGHT_SIGN = -1        # EM2 is mounted mirrored: forward = drive_speed(+v, -v)

# Sensor reads on the mBot2 base hang (no reply) when the base isn't detected,
# so check the shield first and only read the base when it's there.
SHIELD_EXPR = "cyberpi.get_shield()"
SHIELD_EVERY_S = 2.0
# List form keeps the script under the CyberPi's ~255-byte limit (see cyberlink.MAX_SCRIPT).
CYBERPI_KEYS = ("light", "loud")
CYBERPI_EXPR = "[cyberpi.get_bri(), cyberpi.get_loudness('maximum')]"
TELEMETRY_KEYS = ("dist", "line", "batt", "roll", "pitch", "em1", "em2", "light", "loud")
TELEMETRY_EXPR = (
    "[cyberpi.ultrasonic2.get(1), cyberpi.quad_rgb_sensor.get_line_sta(1), cyberpi.get_battery(),"
    " cyberpi.get_roll(), cyberpi.get_pitch(), mbot2.EM_get_speed('EM1'), mbot2.EM_get_speed('EM2'),"
    " cyberpi.get_bri(), cyberpi.get_loudness('maximum')]"
)


class RedetectRequested(Exception):
    pass


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


class BaseRobot:
    """Command interface the web side uses, whatever the transport."""

    def __init__(self):
        self.target = (0, 0)
        self.last_drive = 0.0

    def drive(self, left: float, right: float) -> None:
        self.target = (int(clamp(left, -MAX_RPM, MAX_RPM)), int(clamp(right, -MAX_RPM, MAX_RPM)))
        self.last_drive = time.monotonic()

    def stop(self) -> None:
        self.target = (0, 0)
        self.last_drive = 0.0

    def current_target(self) -> tuple[int, int]:
        return self.target if time.monotonic() - self.last_drive < WATCHDOG_S else (0, 0)

    # overridden per transport
    def led(self, r: int, g: int, b: int) -> None: ...
    def led_off(self) -> None: ...
    def beep(self, freq: int) -> None: ...
    def say(self, text: str) -> None: ...
    def redetect(self) -> None: ...
    async def start(self) -> None: ...
    async def close(self) -> None: ...


# ---- USB serial ---------------------------------------------------------------

class SerialRobot(BaseRobot, threading.Thread):
    def __init__(self, port: str | None, publish):
        BaseRobot.__init__(self)
        threading.Thread.__init__(self, name="robot", daemon=True)
        self.port = port
        self.publish = publish  # thread-safe callback(dict)
        self.sent = None  # last (l, r) actually sent
        self.base_on = False  # mBot2 base (motors/sensors) detected
        self.actions: queue.Queue[str] = queue.Queue()
        self.stop_evt = threading.Event()
        self.redetect_evt = threading.Event()

    def led(self, r, g, b):
        self.actions.put(f"cyberpi.led.on({r}, {g}, {b})")

    def led_off(self):
        self.actions.put("cyberpi.led.off()")

    def beep(self, freq):
        self.actions.put(f"cyberpi.audio.play_tone({freq}, 0.15)")

    def say(self, text):
        # repr() yields a safe Python string literal for on-device eval
        self.actions.put(f"cyberpi.console.clear() or cyberpi.console.println({text!r})")

    def redetect(self):
        self.stop()
        self.redetect_evt.set()

    async def start(self):
        threading.Thread.start(self)

    async def close(self):
        self.stop_evt.set()
        await asyncio.to_thread(self.join, 3)

    def run(self) -> None:
        from cyberlink import CyberPi  # pyserial only needed in serial mode

        while not self.stop_evt.is_set():
            bot = None
            try:
                self.publish({"type": "link", "state": "connecting"})
                bot = CyberPi(self.port)
                bot.connect()
                self.publish({"type": "link", "state": "up", "via": bot.port})
                self._loop(bot)
            except Exception as e:  # serial unplugged, timeouts, etc.
                log.warning("robot link error: %s", e)
                self.publish({"type": "link", "state": "down", "error": str(e)})
            finally:
                if bot:
                    # drive_speed(0, 0), not EM_stop('ALL'): EM_stop is a suspect
                    # for the base dropping out of detection.
                    if self.base_on:
                        try:
                            bot.call("mbot2.drive_speed(0, 0)", timeout=0.5)
                        except Exception:
                            pass
                    bot.close()
                self.sent = None
                self.base_on = False
            # Port closed for a few seconds before reopening: the sequence that
            # preceded the base being re-detected every time it recovered.
            self.stop_evt.wait(3)

    def _loop(self, bot) -> None:
        next_tel = next_shield = 0.0
        shield = None
        misses = 0
        self.base_on = False
        while not self.stop_evt.is_set():
            if self.redetect_evt.is_set():
                self.redetect_evt.clear()
                raise RedetectRequested("re-detecting base: closing port, reopening in 3s")
            now = time.monotonic()
            left, right = self.current_target()
            if (left, right) != self.sent and self.base_on:
                bot.call(f"mbot2.drive_speed({left}, {RIGHT_SIGN * right})", timeout=1)
                self.sent = (left, right)
            while not self.actions.empty():
                script = self.actions.get_nowait()
                try:
                    bot.call(script, timeout=3)
                except (TimeoutError, ValueError) as e:
                    log.info("action failed (%s): %s", e, script)
            if now >= next_tel:
                next_tel = now + TELEMETRY_S
                try:
                    if now >= next_shield:
                        next_shield = now + SHIELD_EVERY_S
                        shield = bot.call(SHIELD_EXPR, timeout=1)
                    keys, expr = (TELEMETRY_KEYS, TELEMETRY_EXPR) if shield == "mbot2" else (CYBERPI_KEYS, CYBERPI_EXPR)
                    vals = bot.call(expr, timeout=1)
                    misses = 0
                    if isinstance(vals, list) and len(vals) == len(keys):
                        self.publish({"type": "telemetry", "shield": shield, **dict(zip(keys, vals)), "drive": self.sent})
                    if self.base_on != (shield == "mbot2"):
                        self.base_on = shield == "mbot2"
                        self.sent = None  # resend drive state after the base appears
                        log.info("mBot2 base %s", "on" if self.base_on else "off")
                except TimeoutError:
                    misses += 1
                    self.publish({"type": "telemetry", "shield": "unknown", "drive": self.sent})
                    if misses >= 10:
                        raise TimeoutError("robot stopped answering")
            time.sleep(0.02)


# ---- Wi-Fi (robot connects in over TCP) ---------------------------------------

def parse_telemetry(line: str) -> dict | None:
    """'T d,line,batt,roll,pitch,em1,em2,light,loud,shield' -> telemetry dict."""
    fields = line[2:].split(",")
    if len(fields) != len(TELEMETRY_KEYS) + 1:
        return None
    out: dict = {"type": "telemetry", "shield": fields[-1]}
    for k, v in zip(TELEMETRY_KEYS, fields[:-1]):
        try:
            out[k] = float(v) if "." in v else int(v)
        except ValueError:
            out[k] = None
    return out


class NetRobot(BaseRobot):
    def __init__(self, host: str, port: int, token: str, publish):
        super().__init__()
        self.host, self.port, self.token = host, port, token
        self.publish = publish
        self.writer: asyncio.StreamWriter | None = None
        self.peer = None
        self.server: asyncio.base_events.Server | None = None
        self.tasks: list[asyncio.Task] = []

    def _send(self, line: str) -> None:
        if self.writer and not self.writer.is_closing():
            self.writer.write((line + "\n").encode())

    def led(self, r, g, b):
        self._send(f"L {r} {g} {b}")

    def led_off(self):
        self._send("O")

    def beep(self, freq):
        self._send(f"B {freq}")

    def say(self, text):
        self._send("S " + "".join(c for c in text if c.isprintable())[:40])

    def redetect(self):
        # Over Wi-Fi the CyberPi runs from the base battery, so it boots with the
        # base present; nothing to re-detect from here.
        self.stop()

    async def start(self):
        self.server = await asyncio.start_server(self._on_robot, self.host, self.port)
        self.tasks.append(asyncio.create_task(self._drive_loop()))
        log.info("waiting for robot on %s:%d", self.host, self.port)
        self.publish({"type": "link", "state": "down", "error": "waiting for robot to connect over Wi-Fi"})

    async def close(self):
        for t in self.tasks:
            t.cancel()
        if self.writer:
            self.writer.close()
        if self.server:
            self.server.close()

    async def _drive_loop(self):
        """Send drive targets ~10x/s while moving; a keepalive every second otherwise."""
        last_sent, last_ping = None, 0.0
        while True:
            await asyncio.sleep(0.1)
            if not self.writer:
                last_sent = None
                continue
            now = time.monotonic()
            target = self.current_target()
            if target != (0, 0) or target != last_sent:
                self._send(f"D {target[0]} {target[1]}")
                last_sent = target
            elif now - last_ping >= 1.0:
                self._send("P")
                last_ping = now
            try:
                await self.writer.drain()
            except (ConnectionError, AttributeError):
                pass

    async def _on_robot(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            hello = (await asyncio.wait_for(reader.readline(), 5)).decode(errors="replace").strip()
        except (asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        if not hello.startswith("H ") or not hmac.compare_digest(hello[2:], self.token):
            log.warning("rejected robot connection from %s (bad token)", peer)
            writer.close()
            return
        if self.writer:  # newer connection wins (robot rebooted)
            self.writer.close()
        self.writer, self.peer = writer, peer
        self.stop()
        log.info("robot connected from %s", peer)
        self.publish({"type": "link", "state": "up", "via": f"Wi-Fi {peer[0]}"})
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), 5)
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text.startswith("T "):
                    tel = parse_telemetry(text)
                    if tel:
                        tel["drive"] = self.current_target()
                        self.publish(tel)
        except (asyncio.TimeoutError, ConnectionError) as e:
            log.info("robot link lost: %r", e)
        finally:
            if self.writer is writer:
                self.writer = None
                self.stop()
                log.info("robot disconnected")
                self.publish({"type": "link", "state": "down", "error": "robot disconnected; waiting for it to reconnect"})
            writer.close()


# ---- web ----------------------------------------------------------------------

def handle_client_msg(robot: BaseRobot, msg: dict) -> None:
    kind = msg.get("type")
    if kind == "drive":
        robot.drive(float(msg.get("l", 0)), float(msg.get("r", 0)))
    elif kind == "stop":
        robot.stop()
    elif kind == "led":
        robot.led(*(int(clamp(int(msg.get(k, 0)), 0, 255)) for k in "rgb"))
    elif kind == "led_off":
        robot.led_off()
    elif kind == "beep":
        robot.beep(int(clamp(int(msg.get("freq", 880)), 100, 4000)))
    elif kind == "say":
        robot.say(str(msg.get("text", ""))[:40])
    elif kind == "redetect":
        log.info("re-detect requested")
        robot.redetect()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    app = request.app
    ws = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)
    app["clients"].add(ws)
    if app["state"]["last_link"]:
        await ws.send_json(app["state"]["last_link"])
    try:
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                try:
                    handle_client_msg(app["robot"], json.loads(m.data))
                except (ValueError, TypeError) as e:
                    log.info("bad client message %r: %s", m.data[:100], e)
    finally:
        app["clients"].discard(ws)
        if not app["clients"]:
            app["robot"].stop()
    return ws


async def index(_request):
    return web.FileResponse(HERE / "static" / "index.html")


def make_app(args) -> web.Application:
    app = web.Application()
    app["clients"] = set()
    app["state"] = {"last_link": None}  # mutable holder; app keys freeze after startup
    loop: asyncio.AbstractEventLoop | None = None  # set in on_startup

    async def broadcast(payload: dict) -> None:
        if payload.get("type") == "link":
            app["state"]["last_link"] = payload
        for ws in list(app["clients"]):
            if not ws.closed:
                await ws.send_json(payload)

    def publish(payload: dict) -> None:
        """Thread-safe: callable from the serial thread or the event loop."""
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

    if args.robot_port:
        token = os.environ.get("MBOT_TOKEN", "")
        if len(token) < 8:
            raise SystemExit("Wi-Fi mode needs MBOT_TOKEN (8+ chars) in the environment")
        app["robot"] = NetRobot(args.robot_host, args.robot_port, token, publish)
    else:
        app["robot"] = SerialRobot(args.serial, publish)

    async def on_startup(_app):
        nonlocal loop
        loop = asyncio.get_running_loop()
        await app["robot"].start()

    async def on_cleanup(_app):
        await app["robot"].close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", HERE / "static")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="web UI bind address")
    ap.add_argument("--port", type=int, default=8088, help="web UI port")
    ap.add_argument("--serial", help="USB mode: serial device (default: auto-detect CH340)")
    ap.add_argument("--robot-port", type=int, help="Wi-Fi mode: TCP port the robot connects to")
    ap.add_argument("--robot-host", default="0.0.0.0", help="Wi-Fi mode: robot listener bind address")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(make_app(args), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
