#!/usr/bin/env python3
import os
import threading
import time
import json
import queue
import signal
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, render_template, jsonify, request

try:
    import yaml
except ImportError:
    yaml = None

try:
    import ntplib
except Exception:
    ntplib = None

# ---------------- Config ----------------
DEFAULT_CONFIG = {
    "target_url": "http://127.0.0.1:8000/",
    "css_selector": "div.Json-Text",
    "poll_interval_sec": 1,
    "bind_host": "127.0.0.1",
    "bind_port": 5000,
    "write_latest_to_runtime_file": True,
    "runtime_file_path": "",  # if blank, defaults to /run/user/$UID/current_location.json
    "request_timeout_sec": 5,
    # Dotted JSON keys
    "latitude_key": "location.latitude",
    "longitude_key": "location.longitude",
    "altitude_key": "location.altitudeMeters",
    "gps_time_key": "location.gpsTimeS",
    # GPS->UTC conversion (GPS time is ahead of UTC by leap seconds)
    "gps_leap_seconds": 18,
    # NTP
    "ntp_server": "time.nist.gov",
    "ntp_refresh_sec": 30
}

def load_config():
    cfg_path = Path("config.yaml")
    cfg = DEFAULT_CONFIG.copy()
    if cfg_path.exists():
        if yaml is None:
            print("[WARN] config.yaml present but PyYAML not installed; using defaults.")
        else:
            with cfg_path.open("r", encoding="utf-8") as f:
                try:
                    user_cfg = yaml.safe_load(f) or {}
                    cfg.update(user_cfg)
                except Exception as e:
                    print(f"[WARN] Failed to parse config.yaml: {e}. Using defaults.")
    if not cfg.get("runtime_file_path"):
        uid = os.getuid()
        cfg["runtime_file_path"] = f"/run/user/{uid}/current_location.json"
    return cfg

CFG = load_config()

# ---------------- Helpers ----------------
def get_by_path(obj, dotted):
    if not dotted:
        return None
    parts = dotted.split(".")
    cur = obj
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur

def coerce_float(val):
    try:
        return float(val)
    except Exception:
        return None

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)

def gps_seconds_to_utc(gps_seconds: float, leap_seconds: int) -> datetime:
    """Convert GPS seconds since 1980-01-06 to UTC datetime by subtracting leap seconds."""
    try:
        dt_gps = GPS_EPOCH + timedelta(seconds=float(gps_seconds))
        dt_utc = dt_gps - timedelta(seconds=int(leap_seconds))
        return dt_utc
    except Exception:
        return None

# ---------------- App State ----------------
app = Flask(__name__)
_updates_queue = queue.Queue()
_state_lock = threading.Lock()
_state = {
    "latitude": None,
    "longitude": None,
    "altitude": None,
    "last_raw": None,
    "last_update_iso": None,
    "note": "Waiting for first update...",
    "runtime_file_path": CFG["runtime_file_path"],
    "src_url": CFG["target_url"],
    # time metrics
    "gps_time_iso": None,
    "pc_time_iso": None,
    "ntp_time_iso": None,
    "delta_pc_vs_gps_ms": None,
    "delta_ntp_vs_pc_ms": None,
    "delta_ntp_vs_gps_ms": None,
}

_ntp_lock = threading.Lock()
_last_ntp_utc = None

def _write_runtime_file(lat, lon, alt):
    if not CFG.get("write_latest_to_runtime_file", True):
        return
    payload = {
        "latitude": lat,
        "longitude": lon,
        "altitude": alt,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        runtime_path = Path(CFG["runtime_file_path"])
        runtime_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = runtime_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(runtime_path)
    except Exception as e:
        print(f"[WARN] Could not write runtime file: {e}")

def fetch_ntp_time():
    global _last_ntp_utc
    if ntplib is None:
        return
    try:
        client = ntplib.NTPClient()
        resp = client.request(CFG.get("ntp_server", "time.nist.gov"), version=3, timeout=5)
        with _ntp_lock:
            _last_ntp_utc = datetime.fromtimestamp(resp.tx_time, tz=timezone.utc)
    except Exception as e:
        # leave last NTP as-is
        pass

def ntp_thread():
    if ntplib is None:
        return
    refresh = max(5, int(CFG.get("ntp_refresh_sec", 30)))
    while True:
        fetch_ntp_time()
        time.sleep(refresh)

def fetch_and_parse_once():
    """Fetch target_url, extract JSON within <div class="Json-Text">, parse lat/lon/alt and GPS time."""
    url = CFG["target_url"]
    sel = CFG["css_selector"]
    try:
        r = requests.get(url, timeout=CFG["request_timeout_sec"])
        r.raise_for_status()
    except Exception as e:
        return {"ok": False, "error": f"HTTP error: {e}"}

    try:
        soup = BeautifulSoup(r.text, "html.parser")
        node = soup.select_one(sel)
        if node is None:
            return {"ok": False, "error": f"Selector '{sel}' not found"}
        raw_text = node.get_text(strip=True)
        data = json.loads(raw_text)
    except Exception as e:
        return {"ok": False, "error": f"Parse error: {e}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "JSON root is not an object"}

    lat_raw = get_by_path(data, CFG.get("latitude_key", "latitude"))
    lon_raw = get_by_path(data, CFG.get("longitude_key", "longitude"))
    alt_raw = get_by_path(data, CFG.get("altitude_key", ""))
    gps_raw = get_by_path(data, CFG.get("gps_time_key", ""))

    lat = coerce_float(lat_raw)
    lon = coerce_float(lon_raw)
    alt = coerce_float(alt_raw) if alt_raw is not None else None

    # time fields
    gps_utc = None
    if gps_raw is not None:
        gps_utc = gps_seconds_to_utc(gps_raw, CFG.get("gps_leap_seconds", 18))

    if lat is None or lon is None:
        return {"ok": False, "error": "Latitude/longitude not found or invalid with configured key paths"}

    return {"ok": True, "latitude": lat, "longitude": lon, "altitude": alt, "gps_utc": gps_utc, "raw": data}

def poller_thread():
    last_triplet = None
    poll = CFG["poll_interval_sec"]
    while True:
        res = fetch_and_parse_once()
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        with _ntp_lock:
            ntp_utc = _last_ntp_utc

        if res["ok"]:
            triplet = (res["latitude"], res["longitude"], res.get("altitude"))
            if triplet != last_triplet:
                with _state_lock:
                    _state["latitude"] = res["latitude"]
                    _state["longitude"] = res["longitude"]
                    _state["altitude"] = res.get("altitude")
                    _state["last_raw"] = res["raw"]
                    _state["last_update_iso"] = now_iso
                    _state["note"] = "Location updated."
                last_triplet = triplet
                _write_runtime_file(*triplet)
                _updates_queue.put({"event": "update", "data": triplet, "time": now_iso})
            else:
                with _state_lock:
                    _state["note"] = "No new location update."

            # time metrics
            gps_utc = res.get("gps_utc")
            with _state_lock:
                _state["pc_time_iso"] = now_iso
                _state["gps_time_iso"] = gps_utc.isoformat() if gps_utc else None
                _state["ntp_time_iso"] = ntp_utc.isoformat() if ntp_utc else None
                if gps_utc:
                    _state["delta_pc_vs_gps_ms"] = int((now_utc - gps_utc).total_seconds() * 1000)
                else:
                    _state["delta_pc_vs_gps_ms"] = None
                if ntp_utc:
                    _state["delta_ntp_vs_pc_ms"] = int((ntp_utc - now_utc).total_seconds() * 1000)
                    if gps_utc:
                        _state["delta_ntp_vs_gps_ms"] = int((ntp_utc - gps_utc).total_seconds() * 1000)
                    else:
                        _state["delta_ntp_vs_gps_ms"] = None
                else:
                    _state["delta_ntp_vs_pc_ms"] = None
                    _state["delta_ntp_vs_gps_ms"] = None

        else:
            with _state_lock:
                _state["note"] = f"Error: {res['error']}"

        time.sleep(max(0.2, poll))

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def api_state():
    with _state_lock:
        return jsonify(_state)

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    """Allow reading/updating runtime config (target_url, selector, keys). Persist to config.yaml."""
    if request.method == "GET":
        public = {k: v for k, v in CFG.items() if k not in ("runtime_file_path",)}
        public["runtime_file_path"] = CFG["runtime_file_path"]
        return jsonify(public)
    else:
        body = request.json or {}
        allowed = [
            "target_url", "css_selector",
            "latitude_key", "longitude_key", "altitude_key", "gps_time_key",
            "gps_leap_seconds", "ntp_server", "poll_interval_sec"
        ]
        changed = {}
        for k in allowed:
            if k in body:
                CFG[k] = body[k]
                changed[k] = body[k]

        # update derived state
        with _state_lock:
            _state["src_url"] = CFG["target_url"]

        # persist to config.yaml
        if yaml is not None:
            cfg_path = Path("config.yaml")
            try:
                # merge with existing file to preserve other fields
                if cfg_path.exists():
                    with cfg_path.open("r", encoding="utf-8") as f:
                        on_disk = yaml.safe_load(f) or {}
                else:
                    on_disk = {}
                on_disk.update(changed)
                with cfg_path.open("w", encoding="utf-8") as f:
                    yaml.safe_dump({**DEFAULT_CONFIG, **on_disk}, f, sort_keys=False)
            except Exception as e:
                return jsonify({"ok": False, "error": f"Failed to write config.yaml: {e}"}), 500

        return jsonify({"ok": True, "changed": changed})

@app.route("/stream")
def stream():
    def event_stream():
        with _state_lock:
            if _state["latitude"] is not None and _state["longitude"] is not None:
                first = {"event": "update",
                         "data": [_state["latitude"], _state["longitude"], _state.get("altitude")],
                         "time": _state["last_update_iso"]}
                yield f"event: {first['event']}\n"
                yield f"data: {json.dumps(first)}\n\n"
        while True:
            item = _updates_queue.get()
            yield f"event: {item['event']}\n"
            yield f"data: {json.dumps(item)}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")

def start_threads():
    t = threading.Thread(target=poller_thread, name="poller", daemon=True)
    t.start()
    if ntplib is not None:
        t2 = threading.Thread(target=ntp_thread, name="ntp", daemon=True)
        t2.start()

def main():
    start_threads()
    def handle_sigterm(signum, frame):
        print("Received SIGTERM, exiting...")
        os._exit(0)
    signal.signal(signal.SIGTERM, handle_sigterm)
    app.run(host=CFG["bind_host"], port=CFG["bind_port"], threaded=True)

if __name__ == "__main__":
    main()
