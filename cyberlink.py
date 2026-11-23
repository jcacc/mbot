"""Minimal client for the CyberPi (mBot2) "online mode" serial protocol.

The CyberPi firmware accepts framed MicroPython expressions over USB serial,
evaluates them on the device, and replies with the result. This replaces
Makeblock's `makeblock`/`cyberpi` PyPI packages (busy-loop threads, eval() on
device replies) with a small, threaded, request/response client.

Frame layout (both directions):
    f3 | hdr_ck | len_lo | len_hi | type | mode | idx_lo | idx_hi | payload... | ck | f4
    hdr_ck = (0xf3 + len_lo + len_hi) & 0xff
    len    = len(payload) + 4   (type, mode, idx_lo, idx_hi)
    ck     = (type + mode + idx_lo + idx_hi + sum(payload)) & 0xff
Script payload: script_len_lo | script_len_hi | utf8 script
"""

from __future__ import annotations

import ast
import json
import logging
import threading
import time
from concurrent.futures import Future

import serial
import serial.tools.list_ports

log = logging.getLogger("cyberlink")

TYPE_SCRIPT = 0x28
TYPE_SUBSCRIBE = 0x29
MODE_NO_RESPONSE = 0x00
MODE_WITH_RESPONSE = 0x01

# "go to online mode" packet, straight from Makeblock's library.
ONLINE_PACKET = bytes([0xF3, 0xF6, 0x03, 0x00, 0x0D, 0x00, 0x01, 0x0E, 0xF4])
OFFLINE_PACKET = bytes([0xF3, 0xF6, 0x03, 0x00, 0x0D, 0x00, 0x00, 0x0D, 0xF4])

CH340_VID_PID = (0x1A86, 0x7523)


def find_port() -> str | None:
    """Return the first CH340 serial port (the CyberPi's USB bridge)."""
    for p in serial.tools.list_ports.comports():
        if (p.vid, p.pid) == CH340_VID_PID:
            return p.device
    return None


def build_frame(ftype: int, mode: int, idx: int, payload: bytes) -> bytes:
    length = len(payload) + 4
    lo, hi = length & 0xFF, (length >> 8) & 0xFF
    body = bytes([ftype, mode, idx & 0xFF, (idx >> 8) & 0xFF]) + payload
    return bytes([0xF3, (0xF3 + lo + hi) & 0xFF, lo, hi]) + body + bytes([sum(body) & 0xFF, 0xF4])


def script_frame(script: str, idx: int, want_response: bool = True) -> bytes:
    data = script.encode("utf8")
    payload = bytes([len(data) & 0xFF, (len(data) >> 8) & 0xFF]) + data
    return build_frame(TYPE_SCRIPT, MODE_WITH_RESPONSE if want_response else MODE_NO_RESPONSE, idx, payload)


def parse_value(text: str):
    """Decode a reply payload without eval(): JSON first, then Python literals."""
    text = text.strip()
    for loader in (json.loads, ast.literal_eval):
        try:
            return loader(text)
        except (ValueError, SyntaxError):
            continue
    return text


class CyberPi:
    def __init__(self, port: str | None = None, baud: int = 115200):
        self.port = port or find_port()
        if not self.port:
            raise RuntimeError("No CyberPi (CH340 USB serial) found")
        # DTR/RTS must be released before open: macOS asserts both by default,
        # which holds the ESP32 in reset (silent port). Opening with them low
        # lets it boot; the CH340 still pulses a power-on reset, so wait ~3s.
        self.ser = serial.Serial()
        self.ser.port, self.ser.baudrate, self.ser.timeout = self.port, baud, 0.05
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        time.sleep(3)
        self.ser.reset_input_buffer()
        self._wlock = threading.Lock()
        self._idx = 0
        self._pending: dict[int, Future] = {}
        self._plock = threading.Lock()
        self._stop = threading.Event()
        self.raw_log: list[bytes] = []  # recent raw frames, for debugging
        self._reader = threading.Thread(target=self._read_loop, name="cyberpi-rx", daemon=True)
        self._reader.start()

    # ---- connection -------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> None:
        """Put the CyberPi in online mode and wait until it answers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._write(ONLINE_PACKET)
            try:
                self.call("cyberpi.get_bri()", timeout=0.5)
                return
            except TimeoutError:
                continue
        raise TimeoutError(f"CyberPi on {self.port} did not respond")

    def close(self) -> None:
        self._stop.set()
        self._reader.join(timeout=1)
        self.ser.close()

    # ---- requests ---------------------------------------------------------
    def _next_idx(self) -> int:
        self._idx = self._idx % 0xFFFF + 1
        return self._idx

    def _write(self, data: bytes) -> None:
        with self._wlock:
            self.ser.write(data)

    def call(self, script: str, timeout: float = 2.0):
        """Evaluate `script` on the CyberPi and return the decoded result."""
        fut: Future = Future()
        with self._plock:
            idx = self._next_idx()
            self._pending[idx] = fut
        self._write(script_frame(script, idx, True))
        try:
            return fut.result(timeout)
        except TimeoutError:
            raise TimeoutError(f"no reply to {script!r}") from None
        finally:
            with self._plock:
                self._pending.pop(idx, None)

    def send(self, script: str) -> None:
        """Fire-and-forget: run `script` without waiting for a reply."""
        with self._plock:
            idx = self._next_idx()
        self._write(script_frame(script, idx, False))

    # ---- receive ----------------------------------------------------------
    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._stop.is_set():
            try:
                chunk = self.ser.read(256)
            except (serial.SerialException, OSError) as e:
                log.error("serial read failed: %s", e)
                break
            if not chunk:
                continue
            buf += chunk
            while True:
                frame = self._take_frame(buf)
                if frame is None:
                    break
                self._handle(frame)

    @staticmethod
    def _take_frame(buf: bytearray) -> bytes | None:
        """Pop one complete frame from the front of buf, discarding junk."""
        while True:
            start = buf.find(0xF3)
            if start < 0:
                buf.clear()
                return None
            del buf[:start]
            if len(buf) < 4:
                return None
            if (0xF3 + buf[2] + buf[3]) & 0xFF != buf[1]:
                del buf[0]  # not a real header; resync
                continue
            total = 4 + (buf[2] | buf[3] << 8) + 2
            if len(buf) < total:
                return None
            frame = bytes(buf[:total])
            del buf[:total]
            if frame[-1] != 0xF4:
                continue
            return frame

    def _handle(self, frame: bytes) -> None:
        self.raw_log = (self.raw_log + [frame])[-50:]
        ftype = frame[4]
        if ftype != TYPE_SCRIPT:
            log.debug("non-script frame type=0x%02x %s", ftype, frame.hex(" "))
            return
        idx = frame[6] | frame[7] << 8
        body = frame[8:-2]
        # Replies seem to carry a 2-byte length prefix like requests do; strip it if it matches.
        if len(body) >= 2 and (body[0] | body[1] << 8) == len(body) - 2:
            body = body[2:]
        text = body.decode("utf8", "replace")
        value = parse_value(text)
        if isinstance(value, dict) and "ret" in value:
            value = value["ret"]
        with self._plock:
            fut = self._pending.get(idx)
        if fut and not fut.done():
            fut.set_result(value)
        else:
            log.debug("unmatched reply idx=%d: %r", idx, text)
