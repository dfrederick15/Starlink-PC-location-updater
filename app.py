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

DEFAULT_CONFIG = {
    "target_url": "http://127.0.0.1:8000/",
    "css_selector": "div.Json-Text",
    "poll_interval_sec": 1,
    "bind_host": "0.0.0.0",
    "bind_port": 5000,
    "write_latest_to_runtime_file": True,
    "runtime_file_path": "",
    "request_timeout_sec": 5,
    "latitude_key": "location.latitude",
    "longitude_key": "location.longitude",
    "altitude_key": "location.altitudeMeters",
    "gps_time_key": "location.gpsTimeS",
    "gps_leap_seconds": 18,
    "ntp_server": "time.nist.gov",
    "ntp_refresh_sec": 30
}

def load_config():
    cfg_path = Path("config.yaml")
    cfg = DEFAULT_CONFIG.copy()
    if cfg_path.exists() and yaml is not None:
        with cfg_path.open("r", encoding="utf-8") as f:
            try:
                cfg.update(yaml.safe_load(f) or {})
            except Exception as e:
                print(f"[WARN] config.yaml parse failed: {e}")
    if not cfg.get("runtime_file_path"):
        uid = os.getuid()
        cfg["runtime_file_path"] = f"/run/user/{uid}/current_location.json"
    return cfg

CFG = load_config()

def get_by_path(obj, dotted):
    if not dotted:
        return None
    cur = obj
    for p in dotted.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur

def coerce_float(v):
    try: return float(v)
    except Exception: return None

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)

def gps_seconds_to_utc(gps_seconds, leap_seconds):
    try:
        dt_gps = GPS_EPOCH + timedelta(seconds=float(gps_seconds))
        return dt_gps - timedelta(seconds=int(leap_seconds))
    except Exception:
        return None

app = Flask(__name__)
_updates = queue.Queue()
_lock = threading.Lock()
_state = {
    "latitude": None,
    "longitude": None,
    "altitude": None,
    "last_raw": None,
    "last_update_iso": None,
    "note": "Waiting for first update...",
    "runtime_file_path": CFG["runtime_file_path"],
    "src_url": CFG["target_url"],
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
        "latitude": lat, "longitude": lon, "altitude": alt,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }
    try:
        path = Path(CFG["runtime_file_path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"[WARN] runtime file write failed: {e}")

def fetch_ntp_time():
    global _last_ntp_utc
    if ntplib is None: return
    try:
        client = ntplib.NTPClient()
        resp = client.request(CFG.get("ntp_server", "time.nist.gov"), version=3, timeout=5)
        with _ntp_lock:
            _last_ntp_utc = datetime.fromtimestamp(resp.tx_time, tz=timezone.utc)
    except Exception:
        pass

def ntp_thread():
    if ntplib is None: return
    refresh = max(5, int(CFG.get("ntp_refresh_sec", 30)))
    while True:
        fetch_ntp_time()
        time.sleep(refresh)

def fetch_and_parse_once():
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
        raw = node.get_text(strip=True)
        data = json.loads(raw)
    except Exception as e:
        return {"ok": False, "error": f"Parse error: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "JSON root is not an object"}
    lat = coerce_float(get_by_path(data, CFG["latitude_key"]))
    lon = coerce_float(get_by_path(data, CFG["longitude_key"]))
    alt_raw = get_by_path(data, CFG["altitude_key"])
    alt = coerce_float(alt_raw) if alt_raw is not None else None
    gps_raw = get_by_path(data, CFG["gps_time_key"])
    gps_utc = gps_seconds_to_utc(gps_raw, CFG["gps_leap_seconds"]) if gps_raw is not None else None
    if lat is None or lon is None:
        return {"ok": False, "error": "Latitude/longitude missing or invalid"}
    return {"ok": True, "latitude": lat, "longitude": lon, "altitude": alt, "gps_utc": gps_utc, "raw": data}

def poller():
    last = None
    poll = CFG["poll_interval_sec"]
    while True:
        res = fetch_and_parse_once()
        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()
        with _ntp_lock:
            ntp_utc = _last_ntp_utc

        if res["ok"]:
            triplet = (res["latitude"], res["longitude"], res.get("altitude"))
            if triplet != last:
                with _lock:
                    _state.update({
                        "latitude": res["latitude"],
                        "longitude": res["longitude"],
                        "altitude": res.get("altitude"),
                        "last_raw": res["raw"],
                        "last_update_iso": now_iso,
                        "note": "Location updated."
                    })
                last = triplet
                _write_runtime_file(*triplet)
                _updates.put({"event": "update", "data": triplet, "time": now_iso})
            else:
                with _lock:
                    _state["note"] = "No new location update."

            gps_utc = res.get("gps_utc")
            with _lock:
                _state["pc_time_iso"] = now_iso
                _state["gps_time_iso"] = gps_utc.isoformat() if gps_utc else None
                _state["ntp_time_iso"] = ntp_utc.isoformat() if ntp_utc else None
                _state["delta_pc_vs_gps_ms"] = int((now_utc - gps_utc).total_seconds()*1000) if gps_utc else None
                if ntp_utc:
                    _state["delta_ntp_vs_pc_ms"] = int((ntp_utc - now_utc).total_seconds()*1000)
                    _state["delta_ntp_vs_gps_ms"] = int((ntp_utc - gps_utc).total_seconds()*1000) if gps_utc else None
                else:
                    _state["delta_ntp_vs_pc_ms"] = None
                    _state["delta_ntp_vs_gps_ms"] = None
        else:
            with _lock:
                _state["note"] = f"Error: {res['error']}"
        time.sleep(max(0.2, poll))

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/state")
def api_state():
    with _lock:
        return jsonify(_state)

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        public = {k: v for k, v in CFG.items()}
        public["runtime_file_path"] = CFG["runtime_file_path"]
        return jsonify(public)
    body = request.json or {}
    allowed = [
        "target_url","css_selector","latitude_key","longitude_key","altitude_key",
        "gps_time_key","gps_leap_seconds","ntp_server","poll_interval_sec"
    ]
    changed = {}
    for k in allowed:
        if k in body:
            CFG[k] = body[k]
            changed[k] = body[k]
    with _lock:
        _state["src_url"] = CFG["target_url"]
    if yaml is not None:
        cfg_path = Path("config.yaml")
        try:
            on_disk = {}
            if cfg_path.exists():
                on_disk = yaml.safe_load(cfg_path.read_text()) or {}
            on_disk.update(changed)
            cfg_path.write_text(yaml.safe_dump({**DEFAULT_CONFIG, **on_disk}, sort_keys=False), encoding="utf-8")
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed to write config.yaml: {e}"}), 500
    return jsonify({"ok": True, "changed": changed})

@app.route("/stream")
def stream():
    def gen():
        with _lock:
            if _state["latitude"] is not None and _state["longitude"] is not None:
                first = {"event": "update",
                         "data": [_state["latitude"], _state["longitude"], _state.get("altitude")],
                         "time": _state["last_update_iso"]}
                yield f"event: update\n"
                yield f"data: {json.dumps(first)}\n\n"
        while True:
            item = _updates.get()
            yield f"event: {item['event']}\n"
            yield f"data: {json.dumps(item)}\n\n"
    return Response(gen(), mimetype="text/event-stream")

def start_threads():
    threading.Thread(target=poller, name="poller", daemon=True).start()
    if ntplib is not None:
        threading.Thread(target=ntp_thread, name="ntp", daemon=True).start()

def main():
    start_threads()
    def handle_sigterm(signum, frame):
        print("Received SIGTERM, exiting..."); os._exit(0)
    signal.signal(signal.SIGTERM, handle_sigterm)
    app.run(host=CFG["bind_host"], port=CFG["bind_port"], threaded=True)

if __name__ == "__main__":
    main()
