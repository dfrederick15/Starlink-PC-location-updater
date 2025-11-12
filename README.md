# Starlink-PC-Location-Updater

A lightweight Debian-ready Python app that reads live JSON telemetry from an internal status webpage and updates the PC’s location. It also compares GPS, PC, and NIST NTP time and shows drift on a live dashboard.

## Quickstart
```bash
chmod +x install.sh
./install.sh --url http://192.168.1.1/status --service --start
```
Open http://localhost:5000

## Example JSON
```json
{
  "location": {
    "latitude": -10.951121,
    "longitude": -150.393507,
    "altitudeMeters": 339.7,
    "gpsTimeS": 1446994128.52
  }
}
```
