"""mBot2 web control: aiohttp + WebSocket bridge to the CyberPi over USB serial.

    .venv/bin/python server.py              # http://127.0.0.1:8088
    .venv/bin/python server.py --host 0.0.0.0   # reachable on the LAN (no auth!)

One robot thread owns the serial link and runs a fixed loop: apply the latest
drive target, run queued actions (LEDs, sounds, screen), poll telemetry. Browser
clients send drive targets ~10x/s while a control is held; if none arrive for
WATCHDOG_S the robot is stopped, so a closed tab or dropped Wi-Fi can't leave it
driving.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import threading
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from cyberlink import CyberPi

log = logging.getLogger("mbot")

HERE = Path(__file__).parent
MAX_RPM = 200          # mbot2.drive_speed limit
WATCHDOG_S = 0.5       # stop if no drive command for this long
TELEMETRY_S = 0.15     # telemetry poll period
RIGHT_SIGN = -1       # EM2 is mounted mirrored: forward = drive_speed(+v, -v)

# Sensor reads on the mBot2 base hang (no reply) when the base is switched off and
# only the CyberPi is USB-powered, so check the shield first and only read the
# base when it's there.
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


class Robot(threading.Thread):
    def __init__(self, port: str | None, publish):
        super().__init__(name="robot", daemon=True)
        self.port = port
        self.publish = publish  # thread-safe callback(dict)
        self.target = (0, 0)
        self.last_drive = 0.0
        self.sent = None  # last (l, r) actually sent
        self.base_on = False  # mBot2 base (motors/sensors) powered
        self.actions: queue.Queue[str] = queue.Queue()
        self.stop_evt = threading.Event()
        self.redetect = threading.Event()  # reopen the port so the CyberPi reboots and re-detects the base

    def drive(self, left: float, right: float) -> None:
        self.target = (int(clamp(left, -MAX_RPM, MAX_RPM)), int(clamp(right, -MAX_RPM, MAX_RPM)))
        self.last_drive = time.monotonic()

    def act(self, script: str) -> None:
        self.actions.put(script)

    def run(self) -> None:
        while not self.stop_evt.is_set():
            bot = None
            try:
                self.publish({"type": "link", "state": "connecting"})
                bot = CyberPi(self.port)
                bot.connect()
                self.publish({"type": "link", "state": "up", "port": bot.port})
                self._loop(bot)
            except Exception as e:  # serial unplugged, timeouts, etc.
                log.warning("robot link error: %s", e)
                self.publish({"type": "link", "state": "down", "error": str(e)})
            finally:
                if bot:
                    # drive_speed(0, 0), not EM_stop('ALL'): EM_stop is the main
                    # suspect for the base dropping out of detection.
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

    def _loop(self, bot: CyberPi) -> None:
        next_tel = next_shield = 0.0
        shield = None
        misses = 0
        self.base_on = False
        while not self.stop_evt.is_set():
            if self.redetect.is_set():
                self.redetect.clear()
                raise RedetectRequested("re-detecting base: closing port, reopening in 3s")
            now = time.monotonic()
            left, right = self.target if now - self.last_drive < WATCHDOG_S else (0, 0)
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
                        self.sent = None  # resend drive state after the base powers on
                        log.info("mBot2 base %s", "on" if self.base_on else "off")
                except TimeoutError:
                    misses += 1
                    self.publish({"type": "telemetry", "shield": "unknown", "drive": self.sent})
                    if misses >= 10:
                        raise TimeoutError("robot stopped answering")
            time.sleep(0.02)


# ---- web --------------------------------------------------------------------

def safe_text(s: str, limit: int = 40) -> str:
    """Return a Python string literal for on-device eval (repr is a safe literal)."""
    return repr(str(s)[:limit])


def handle_client_msg(robot: Robot, msg: dict) -> None:
    kind = msg.get("type")
    if kind == "drive":
        robot.drive(float(msg.get("l", 0)), float(msg.get("r", 0)))
    elif kind == "stop":
        robot.drive(0, 0)
        robot.last_drive = 0
    elif kind == "led":
        r, g, b = (int(clamp(int(msg.get(k, 0)), 0, 255)) for k in "rgb")
        robot.act(f"cyberpi.led.on({r}, {g}, {b})")
    elif kind == "redetect":
        robot.drive(0, 0)
        robot.last_drive = 0
        robot.redetect.set()
        log.info("re-detect requested")
    elif kind == "led_off":
        robot.act("cyberpi.led.off()")
    elif kind == "beep":
        freq = int(clamp(int(msg.get("freq", 880)), 100, 4000))
        robot.act(f"cyberpi.audio.play_tone({freq}, 0.15)")
    elif kind == "say":
        robot.act(f"cyberpi.console.clear() or cyberpi.console.println({safe_text(msg.get('text', ''))})")


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
            app["robot"].drive(0, 0)
            app["robot"].last_drive = 0
    return ws


async def index(_request):
    return web.FileResponse(HERE / "static" / "index.html")


def make_app(port: str | None) -> web.Application:
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
        if loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(broadcast(payload), loop)

    app["robot"] = Robot(port, publish)

    async def on_startup(_app):
        nonlocal loop
        loop = asyncio.get_running_loop()
        app["robot"].start()

    async def on_cleanup(_app):
        app["robot"].stop_evt.set()
        await asyncio.to_thread(app["robot"].join, 3)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", HERE / "static")
    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--serial", help="serial device (default: auto-detect CH340)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    web.run_app(make_app(args.serial), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
