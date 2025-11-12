#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${APP_DIR}/.venv"
SERVICE_NAME="location-watcher"
SERVICE_FILE="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

usage() {
  cat <<EOF
Usage: ./install.sh [options]

Options:
  --url URL                Target webpage URL (default: from config.yaml)
  --selector CSS           CSS selector for JSON (default: from config.yaml)
  --lat-key KEY            Dotted key for latitude (default: from config.yaml)
  --lon-key KEY            Dotted key for longitude
  --alt-key KEY            Dotted key for altitude
  --gps-key KEY            Dotted key for gpsTimeS
  --ntp-server HOST        NTP server (default: time.nist.gov)
  --service                Install as a systemd user service
  --start                  Start service immediately
  -h, --help               Show this help

Examples:
  ./install.sh --url http://192.168.1.1/status --service --start
EOF
}

URL="" SELECTOR="" LAT_KEY="" LON_KEY="" ALT_KEY="" GPS_KEY="" NTP_SRV="" DO_SERVICE="false" START_AFTER="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url) URL="$2"; shift 2;;
    --selector) SELECTOR="$2"; shift 2;;
    --lat-key) LAT_KEY="$2"; shift 2;;
    --lon-key) LON_KEY="$2"; shift 2;;
    --alt-key) ALT_KEY="$2"; shift 2;;
    --gps-key) GPS_KEY="$2"; shift 2;;
    --ntp-server) NTP_SRV="$2"; shift 2;;
    --service) DO_SERVICE="true"; shift;;
    --start) DO_SERVICE="true"; START_AFTER="true"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown option: $1"; usage; exit 1;;
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
  pip install -r "${APP_DIR}/requirements.txt"
}

apply_config() {
  echo "[*] Updating config.yaml…"
  source "${VENV_DIR}/bin/activate"
  python - <<PY
import yaml
from pathlib import Path
cfg_path = Path("${APP_DIR}") / "config.yaml"
cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
update = {}
if "${URL}": update["target_url"]="${URL}"
if "${SELECTOR}": update["css_selector"]="${SELECTOR}"
if "${LAT_KEY}": update["latitude_key"]="${LAT_KEY}"
if "${LON_KEY}": update["longitude_key"]="${LON_KEY}"
if "${ALT_KEY}": update["altitude_key"]="${ALT_KEY}"
if "${GPS_KEY}": update["gps_time_key"]="${GPS_KEY}"
if "${NTP_SRV}": update["ntp_server"]="${NTP_SRV}"
cfg.update(update)
cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
print("[*] Config updated:", update)
PY
}

install_service() {
  echo "[*] Creating user-level systemd service…"
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
  [[ "${START_AFTER}" == "true" ]] && systemctl --user start "${SERVICE_NAME}.service"
}

main() {
  echo "[*] Installing Location Watcher to ${APP_DIR}"
  apt_install
  venv_setup
  apply_config
  if [[ "${DO_SERVICE}" == "true" ]]; then
    install_service
  else
    echo
    echo "Run manually with:"
    echo "  source \"${VENV_DIR}/bin/activate\""
    echo "  python3 app.py"
    echo "Then open http://localhost:5000"
  fi
}

main "$@"
