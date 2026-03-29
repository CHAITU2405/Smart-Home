import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory


APP_DIR = os.path.dirname(os.path.abspath(__file__))
ZONE_COUNT = 3


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


@dataclass
class ZoneState:
    lights: list[bool] = field(default_factory=lambda: [False, False, False])
    lights_on: int = 0
    last_update_utc: str | None = None
    last_change_utc: str | None = None
    alerts: list[dict] = field(default_factory=list)
    last_http_state: str | None = None
    esp32_last_seen_utc: str | None = None


class ZoneStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.state = ZoneState()

    def snapshot(self) -> dict:
        with self._lock:
            s = self.state
            return {
                "lights_on": s.lights_on,
                "lights": list(s.lights),
                "zone_labels": ["Home", "Kitchen", "Hall"],
                "last_update_utc": s.last_update_utc,
                "last_change_utc": s.last_change_utc,
                "last_http_state": s.last_http_state,
                "esp32_last_seen_utc": s.esp32_last_seen_utc,
                "last_source": "http" if s.esp32_last_seen_utc else None,
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
        zones = _parse_http_state(raw_state)
        now = _utc_iso()
        alert_payload: tuple[str, str, str] | None = None
        with self._lock:
            prev = self.state.lights_on
            new_count = sum(1 for x in zones if x)
            self.state.last_http_state = raw_state.strip() if raw_state else "0"
            self.state.esp32_last_seen_utc = now
            self.state.last_update_utc = now

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


app = Flask(__name__, static_folder=None)
store = ZoneStore()


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
    store.apply_http_state(raw)
    return "OK", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/api/state")
def state():
    return jsonify({"ok": True, "state": store.snapshot()}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
