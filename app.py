import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory

try:
    import serial  # type: ignore
    import serial.tools.list_ports  # type: ignore
except Exception:  # pragma: no cover
    serial = None


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ZONE_COUNT = 3


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pick_default_port() -> str | None:
    if serial is None:
        return None
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        return None
    preferred = None
    for p in ports:
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        if any(k in desc for k in ("arduino", "ch340", "usb serial", "cp210", "ftdi")) or any(
            k in hwid for k in ("arduino", "ch340", "cp210", "ftdi")
        ):
            preferred = p.device
            break
    return preferred or ports[0].device


def _parse_http_state(state_param: str) -> list[bool]:
    """ESP32: ?state=0 | ?state=1 | ?state=1,2 | ?state=1,2,3 → zones [home, kitchen, hall]."""
    text = (state_param or "").strip()
    zones = [False, False, False]
    if not text or text == "0":
        return zones
    for part in text.split(","):
        part = part.strip()
        if part == "1":
            zones[0] = True
        elif part == "2":
            zones[1] = True
        elif part == "3":
            zones[2] = True
    return zones


def _parse_serial_zones(line: str) -> list[bool] | None:
    """
    USB serial (legacy): None / space-separated IDs / single digit 1–3.
    Maps 1→Home, 2→Kitchen, 3→Hall.
    """
    text = line.strip()
    if not text:
        return None
    if text.lower() == "none":
        return [False, False, False]

    if re.fullmatch(r"\d+", text):
        v = int(text)
        if v == 0:
            return [False, False, False]
        if 1 <= v <= 3:
            z = [False, False, False]
            z[v - 1] = True
            return z
        return None

    nums = re.findall(r"\d+", text)
    if not nums:
        return None
    values = [int(n) for n in nums]

    if len(values) > 1 or (len(values) == 1 and any(k in text.lower() for k in ("lights", "count", "on"))):
        if any(k in text.lower() for k in ("lights", "count", "on")):
            n = values[0]
            if 0 <= n <= ZONE_COUNT:
                return [i < n for i in range(ZONE_COUNT)]
        z = [False, False, False]
        for v in values:
            if 1 <= v <= 3:
                z[v - 1] = True
        return z

    if len(values) == 1:
        v = values[0]
        if 1 <= v <= 3:
            z = [False, False, False]
            z[v - 1] = True
            return z
    return None


@dataclass
class SensorState:
    connected: bool = False
    port: str | None = None
    baudrate: int = 9600
    last_raw_line: str | None = None
    lights: list[bool] = field(default_factory=lambda: [False, False, False])
    lights_on: int = 0
    last_update_utc: str | None = None
    last_change_utc: str | None = None
    last_error: str | None = None
    alerts: list[dict] = field(default_factory=list)
    last_source: str | None = None
    last_http_state: str | None = None
    esp32_last_seen_utc: str | None = None


class SerialReader:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = SensorState()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ser = None

    def snapshot(self) -> dict:
        with self._lock:
            s = self.state
            return {
                "connected": s.connected,
                "port": s.port,
                "baudrate": s.baudrate,
                "lights_on": s.lights_on,
                "lights": list(s.lights),
                "zone_labels": ["Home", "Kitchen", "Hall"],
                "last_raw_line": s.last_raw_line,
                "last_update_utc": s.last_update_utc,
                "last_change_utc": s.last_change_utc,
                "last_error": s.last_error,
                "last_source": s.last_source,
                "last_http_state": s.last_http_state,
                "esp32_last_seen_utc": s.esp32_last_seen_utc,
                "alerts": list(s.alerts)[-20:],
            }

    def _push_alert(self, title: str, message: str, level: str = "info") -> None:
        with self._lock:
            self.state.alerts.append(
                {
                    "utc": _utc_iso(),
                    "level": level,
                    "title": title,
                    "message": message,
                }
            )
            if len(self.state.alerts) > 200:
                self.state.alerts = self.state.alerts[-200:]

    def apply_http_state(self, raw_state: str) -> None:
        """Called from GET /update when ESP32 pushes zone state."""
        zones = _parse_http_state(raw_state)
        now = _utc_iso()
        alert_payload: tuple[str, str, str] | None = None
        with self._lock:
            prev = self.state.lights_on
            new_count = sum(1 for x in zones if x)
            self.state.last_http_state = raw_state.strip() if raw_state else "0"
            self.state.esp32_last_seen_utc = now
            self.state.last_source = "http"
            self.state.last_update_utc = now
            self.state.last_raw_line = f"HTTP state={self.state.last_http_state}"

            if zones != self.state.lights:
                self.state.lights = list(zones)
                self.state.lights_on = new_count
                self.state.last_change_utc = now

            if new_count != prev:
                if new_count == ZONE_COUNT:
                    alert_payload = ("All zones ON", "ESP32 reports Home, Kitchen, Hall all ON.", "warning")
                elif new_count == 0:
                    alert_payload = ("All zones OFF", "ESP32 reports all zones OFF.", "info")
                elif new_count > prev:
                    alert_payload = ("Zones increased", f"ESP32 reports {new_count}/{ZONE_COUNT} zones ON.", "warning")
                else:
                    alert_payload = ("Zones decreased", f"ESP32 reports {new_count}/{ZONE_COUNT} zones ON.", "info")

        if alert_payload:
            self._push_alert(*alert_payload)

    def connect(self, port: str | None, baudrate: int) -> tuple[bool, str]:
        if serial is None:
            return False, "pyserial not installed. Install requirements and restart."

        with self._lock:
            if self.state.connected:
                return True, "Already connected."

            self.state.last_error = None
            self.state.baudrate = baudrate
            self.state.port = port or _pick_default_port()

            if not self.state.port:
                return False, "No serial ports found. Plug in board and try again."

        try:
            self._ser = serial.Serial(self.state.port, baudrate=baudrate, timeout=1)
        except Exception as e:
            with self._lock:
                self.state.last_error = str(e)
                self.state.connected = False
            return False, f"Failed to open {self.state.port}: {e}"

        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="serial-reader", daemon=True)
        self._thread.start()

        with self._lock:
            self.state.connected = True
            self.state.last_update_utc = _utc_iso()

        self._push_alert("Serial connected", f"Connected to {self.state.port} @ {baudrate}.", "success")
        return True, f"Connected to {self.state.port}."

    def disconnect(self) -> tuple[bool, str]:
        with self._lock:
            was_connected = self.state.connected
            self.state.connected = False

        self._stop.set()
        try:
            if self._ser:
                try:
                    self._ser.close()
                except Exception:
                    pass
        finally:
            self._ser = None

        if was_connected:
            self._push_alert("Serial disconnected", "Disconnected from USB serial.", "warning")
        return True, "Disconnected."

    def _run(self) -> None:
        while not self._stop.is_set():
            ser = self._ser
            if ser is None:
                time.sleep(0.1)
                continue

            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode(errors="ignore").strip()
            except Exception as e:
                with self._lock:
                    self.state.last_error = str(e)
                    self.state.connected = False
                self._push_alert("Serial error", f"Serial read failed: {e}", "error")
                try:
                    if self._ser:
                        self._ser.close()
                except Exception:
                    pass
                self._ser = None
                return

            parsed = _parse_serial_zones(line)
            now = _utc_iso()
            alert_payload: tuple[str, str, str] | None = None
            with self._lock:
                self.state.last_raw_line = line
                self.state.last_update_utc = now
                self.state.last_source = "serial"

                if parsed is None:
                    continue

                prev = self.state.lights_on
                new_count = sum(1 for x in parsed if x)

                if parsed != self.state.lights:
                    self.state.lights = list(parsed)
                    self.state.lights_on = new_count
                    self.state.last_change_utc = now

                if new_count != prev:
                    if new_count == ZONE_COUNT:
                        alert_payload = ("All zones ON", "Serial reports all 3 zones ON.", "warning")
                    elif new_count == 0:
                        alert_payload = ("All zones OFF", "Serial reports all zones OFF.", "info")
                    elif new_count > prev:
                        alert_payload = ("Zones increased", f"Serial reports {new_count}/{ZONE_COUNT} zones ON.", "warning")
                    else:
                        alert_payload = ("Zones decreased", f"Serial reports {new_count}/{ZONE_COUNT} zones ON.", "info")

            if alert_payload:
                self._push_alert(*alert_payload)


app = Flask(__name__, static_folder=None)
reader = SerialReader()


@app.get("/")
def home():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/script.js")
def js():
    return send_from_directory(APP_DIR, "script.js")


@app.get("/style.css")
def css():
    return send_from_directory(APP_DIR, "style.css")


@app.get("/update")
def esp32_update():
    """
    ESP32 HTTPClient GET: https://YOUR-APP.onrender.com/update?state=1,2
    state=0 → all off; state=1,2,3 comma-separated zone IDs.
    """
    raw = request.args.get("state", "0")
    reader.apply_http_state(raw)
    return "OK", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/api/ports")
def ports():
    if serial is None:
        return jsonify({"ok": False, "ports": [], "message": "pyserial not installed"}), 200
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({"device": p.device, "description": p.description, "hwid": p.hwid})
    return jsonify({"ok": True, "ports": ports, "default": _pick_default_port()}), 200


@app.get("/api/state")
def state():
    return jsonify({"ok": True, "state": reader.snapshot()}), 200


@app.post("/api/connect")
def connect():
    data = request.get_json(silent=True) or {}
    port = data.get("port") or os.environ.get("SMARTLIGHT_PORT") or None
    baudrate = int(data.get("baudrate") or os.environ.get("SMARTLIGHT_BAUD") or 9600)
    ok, msg = reader.connect(port=port, baudrate=baudrate)
    return jsonify({"ok": ok, "message": msg, "state": reader.snapshot()}), 200


@app.post("/api/disconnect")
def disconnect():
    ok, msg = reader.disconnect()
    return jsonify({"ok": ok, "message": msg, "state": reader.snapshot()}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
