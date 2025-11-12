# Create an install.sh script that automates setup on Debian/Ubuntu,
# supports command-line flags to configure the app, and optionally installs a systemd user service.

from pathlib import Path

script = r'''#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="location-watcher"
SERVICE_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

URL_DEFAULT="http://127.0.0.1:8000/"
SELECTOR_DEFAULT="div.Json-Text"
LAT_KEY_DEFAULT="location.latitude"
LON_KEY_DEFAULT="location.longitude"
ALT_KEY_DEFAULT="location.altitudeMeters"
BIND_HOST_DEFAULT="0.0.0.0"
BIND_PORT_DEFAULT="5000"
RUNTIME_FILE_DEFAULT=""

URL=""
SELECTOR=""
LAT_KEY=""
LON_KEY=""
ALT_KEY=""
BIND_HOST=""
BIND_PORT=""
RUNTIME_FILE=""
DO_SERVICE="false"
START_AFTER_INSTALL="false"

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  --url URL                Target webpage URL containing the JSON (default: ${URL_DEFAULT})
  --selector CSS           CSS selector for the JSON container (default: ${SELECTOR_DEFAULT})
  --lat-key KEY            Dotted key path for latitude (default: ${LAT_KEY_DEFAULT})
  --lon-key KEY            Dotted key path for longitude (default: ${LON_KEY_DEFAULT})
  --alt-key KEY            Dotted key path for altitude (default: ${ALT_KEY_DEFAULT})
  --bind-host HOST         Flask bind host (default: ${BIND_HOST_DEFAULT})
  --bind-port PORT         Flask bind port (default: ${BIND_PORT_DEFAULT})
  --runtime-file PATH      Path to write current_location.json (default: /run/user/UID/current_location.json)
  --service                Install a user-level systemd service
  --start                  Start the service after installation (implies --service)
  -h, --help               Show this help

Examples:
  ./install.sh --url http://192.168.1.10/page --selector 'div.Json-Text' --service --start
  ./install.sh --lat-key location.latitude --lon-key location.longitude
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --selector) SELECTOR="$2"; shift 2 ;;
    --lat-key) LAT_KEY="$2"; shift 2 ;;
    --lon-key) LON_KEY="$2"; shift 2 ;;
    --alt-key) ALT_KEY="$2"; shift 2 ;;
    --bind-host) BIND_HOST="$2"; shift 2 ;;
    --bind-port) BIND_PORT="$2"; shift 2 ;;
    --runtime-file) RUNTIME_FILE="$2"; shift 2 ;;
    --service) DO_SERVICE="true"; shift ;;
    --start) DO_SERVICE="true"; START_AFTER_INSTALL="true"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

apt_install() {
  echo "[*] Installing system prerequisites…"
  sudo apt-get update -y
  sudo apt-get install -y python3 python3-venv python3-pip
}

venv_setup() {
  echo "[*] Creating virtual environment…"
  python3 -m venv "${VENV_DIR}"
  # shellcheck disable=SC1090
  source "${VENV_DIR}/bin/activate"
  echo "[*] Installing Python dependencies…"
  pip install --upgrade pip
  if [[ -f "${APP_DIR}/requirements.txt" ]]; then
    pip install -r "${APP_DIR}/requirements.txt"
  else
    # fallback: minimal deps
    pip install flask requests beautifulsoup4 PyYAML
  fi
}

configure_yaml() {
  echo "[*] Applying configuration to config.yaml…"
  local cfg="${APP_DIR}/config.yaml"
  if [[ ! -f "${cfg}" ]]; then
    echo "target_url: \"${URL_DEFAULT}\"" > "${cfg}"
    echo "css_selector: \"${SELECTOR_DEFAULT}\"" >> "${cfg}"
    echo "poll_interval_sec: 1" >> "${cfg}"
    echo "bind_host: \"${BIND_HOST_DEFAULT}\"" >> "${cfg}"
    echo "bind_port: ${BIND_PORT_DEFAULT}" >> "${cfg}"
    echo "write_latest_to_runtime_file: true" >> "${cfg}"
    echo "runtime_file_path: \"\"" >> "${cfg}"
    echo "request_timeout_sec: 5" >> "${cfg}"
    echo "latitude_key: \"${LAT_KEY_DEFAULT}\"" >> "${cfg}"
    echo "longitude_key: \"${LON_KEY_DEFAULT}\"" >> "${cfg}"
    echo "altitude_key: \"${ALT_KEY_DEFAULT}\"" >> "${cfg}"
  fi

  local u="${URL:-${URL_DEFAULT}}"
  local s="${SELECTOR:-${SELECTOR_DEFAULT}}"
  local lat="${LAT_KEY:-${LAT_KEY_DEFAULT}}"
  local lon="${LON_KEY:-${LON_KEY_DEFAULT}}"
  local alt="${ALT_KEY:-${ALT_KEY_DEFAULT}}"
  local bh="${BIND_HOST:-${BIND_HOST_DEFAULT}}"
  local bp="${BIND_PORT:-${BIND_PORT_DEFAULT}}"
  local rf="${RUNTIME_FILE:-${RUNTIME_FILE_DEFAULT}}"

  "${VENV_DIR}/bin/python" - <<PY
import sys, yaml
from pathlib import Path

cfg_path = Path("${APP_DIR}") / "config.yaml"
with cfg_path.open("r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f) or {}

cfg.update({
    "target_url": "${u}",
    "css_selector": "${s}",
    "latitude_key": "${lat}",
    "longitude_key": "${lon}",
    "altitude_key": "${alt}",
    "bind_host": "${bh}",
    "bind_port": int("${bp}"),
})

rf = "${rf}".strip()
if rf:
    cfg["runtime_file_path"] = rf

with cfg_path.open("w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
print("[*] Wrote", cfg_path)
PY
}

write_service() {
  echo "[*] Creating user systemd service ${SERVICE_FILE} …"
  mkdir -p "$(dirname "${SERVICE_FILE}")"
  cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Location Watcher

[Service]
WorkingDirectory=%h/$(basename "${APP_DIR}")
ExecStart=%h/$(basename "${APP_DIR}")/.venv/bin/python %h/$(basename "${APP_DIR}")/app.py
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF

  systemctl --user daemon-reload
  systemctl --user enable "${SERVICE_NAME}.service"
  echo "[*] Service installed. You can start it with: systemctl --user start ${SERVICE_NAME}.service"
  if [[ "${START_AFTER_INSTALL}" == "true" ]]; then
    systemctl --user start "${SERVICE_NAME}.service"
    echo "[*] Service started."
  fi
}

main() {
  echo "[*] App directory: ${APP_DIR}"
  apt_install
  venv_setup
  configure_yaml

  if [[ "${DO_SERVICE}" == "true" ]]; then
    write_service
  else
    echo
    echo "[*] To run now:"
    echo "    source \"${VENV_DIR}/bin/activate\""
    echo "    python \"${APP_DIR}/app.py\""
    echo
    echo "[*] Then open: http://localhost:${BIND_PORT:-${BIND_PORT_DEFAULT}}"
  fi
}

main "$@"
'''

out = Path("/mnt/data/install.sh")
out.write_text(script, encoding="utf-8")
print(str(out))
