#!/usr/bin/env bash
# Schedule the Copart downloader hourly with launchd (macOS), in your user session so it can
# open Chrome. Run once; re-run to update. `--uninstall` removes the schedule.
#
#   ./scripts/install_downloader.sh
#   ./scripts/install_downloader.sh --uninstall
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
LABEL="com.cars-ingest.copart-downloader"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/cars-ingest"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$PLIST"
  echo "Downloader schedule removed."
  exit 0
fi

command -v uv >/dev/null || { echo "error: uv is required (https://docs.astral.sh/uv/)" >&2; exit 1; }
[ -d "/Applications/Google Chrome.app" ] || { echo "error: Google Chrome is required" >&2; exit 1; }
[ -f .env ] || { echo "error: run ./start.sh first (it creates .env)" >&2; exit 1; }
(cd backend && uv sync --quiet --extra downloader)

mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/backend/.venv/bin/python</string>
    <string>-m</string><string>ingest.downloader</string>
    <string>run</string><string>--scheduled</string>
    <string>--every</string><string>3600</string>
    <string>--jitter</string><string>300</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT/backend</string>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><false/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/downloader.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/downloader.log</string>
</dict>
</plist>
PLIST
launchctl bootstrap "$DOMAIN" "$PLIST"

cat <<MSG
Downloader scheduled: every hour (plus up to 5 min random delay), at most once per 45 min.

  Log in first (once):  cd backend && .venv/bin/python -m ingest.downloader login
  Download now:         cd backend && .venv/bin/python -m ingest.downloader run --force
  Status:               cd backend && .venv/bin/python -m ingest.downloader status
  Log:                  $LOG_DIR/downloader.log
  Remove schedule:      ./scripts/install_downloader.sh --uninstall
MSG
